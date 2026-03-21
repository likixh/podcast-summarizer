import os
import re
import json
import time
import feedparser
import requests
import subprocess
import tempfile

# ============================================================
# 配置
# ============================================================
RSS_URL            = "https://www.ximalaya.com/album/80074602.xml"
FEISHU_APP_ID      = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET  = os.environ["FEISHU_APP_SECRET"]
FEISHU_APP_TOKEN   = os.environ["FEISHU_APP_TOKEN"]
FEISHU_TABLE_ID    = os.environ["FEISHU_TABLE_ID"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
FEISHU_WEBHOOK     = os.environ["FEISHU_WEBHOOK"]

MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-4-scout:free",
    "openrouter/free",
]

# ============================================================
# 从 RSS 简介提取 Highlights 和书单
# ============================================================
def extract_from_description(entry):
    """
    从 RSS 简介中提取：
    - Highlights 段落 → 金句
    - 本期学习推荐 段落 → 书单
    返回 (highlights, booklist)，均为字符串，未找到则为空字符串
    """
    description = ""
    if hasattr(entry, "summary"):
        description = entry.summary
    elif hasattr(entry, "content") and entry.content:
        description = entry.content[0].get("value", "")

    if not description:
        return "", ""

    # 清理 HTML
    description = re.sub(r"<[^>]+>", "\n", description)
    description = re.sub(r"&nbsp;", " ", description)
    description = re.sub(r"&amp;", "&", description)
    description = re.sub(r"&#\d+;", "", description)
    description = re.sub(r"\r\n", "\n", description)
    description = re.sub(r"\n{3,}", "\n\n", description)

    print(f"  RSS 简介（前600字）：\n{description[:600]}\n")

    # 提取 Highlights
    highlights = ""
    match = re.search(
        r"[Hh]ighlights[：:\s]*\n([\s\S]+?)(?:\n\n|本期|$)", description
    )
    if match:
        lines = [l.strip() for l in match.group(1).split("\n") if l.strip()]
        highlights = "\n".join(f"• {l.lstrip('•·-· ')}" for l in lines)
        print(f"  ✅ 提取到 Highlights：{len(lines)} 条")
    else:
        print("  ℹ️  未找到 Highlights，将用 AI 提取金句")

    # 提取本期学习推荐
    booklist = ""
    match2 = re.search(
        r"本期学习推荐[：:\s]*\n([\s\S]+?)(?:\n\n|Highlights|[Hh]ighlights|$)",
        description
    )
    if match2:
        lines2 = [l.strip() for l in match2.group(1).split("\n") if l.strip()]
        booklist = "\n".join(f"• {l.lstrip('•·-· ')}" for l in lines2)
        print(f"  ✅ 提取到书单：{len(lines2)} 条")
    else:
        print("  ℹ️  未找到本期学习推荐，将用 AI 提取书单")

    return highlights, booklist


# ============================================================
# 飞书 API
# ============================================================
def get_feishu_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"  飞书 Token：code={data.get('code')} msg={data.get('msg')}")
    return data["tenant_access_token"]


def get_existing_links():
    token = get_feishu_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/"
        f"apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
    )
    resp = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
    data = resp.json()
    print(f"  查询记录：code={data.get('code')} msg={data.get('msg')}")
    records = data.get("data", {}).get("items", [])
    return {r["fields"].get("原链接", "") for r in records}


def write_to_feishu(fields):
    token = get_feishu_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/"
        f"apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
    )
    print(f"  写入字段：{list(fields.keys())}")
    resp = requests.post(url, headers=headers, json={"fields": fields}, timeout=30)
    data = resp.json()
    print(f"  飞书响应：{json.dumps(data, ensure_ascii=False)[:400]}")
    resp.raise_for_status()
    return data


def send_feishu_notification(title, date, link):
    msg = {
        "msg_type": "interactive",
        "card": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**📚 播客新内容已入库！**\n\n"
                            f"**标题：** {title}\n"
                            f"**发布日期：** {date}\n"
                            f"**链接：** {link}"
                        )
                    }
                }
            ],
            "header": {
                "title": {"tag": "plain_text", "content": "🎙️ 自习室播客精华更新"},
                "template": "blue"
            }
        }
    }
    resp = requests.post(FEISHU_WEBHOOK, json=msg, timeout=10)
    print(f"  机器人通知：{resp.json()}")


# ============================================================
# 音频下载
# ============================================================
def download_audio(episode_url, output_path):
    print(f"  下载：{episode_url}")
    subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "32K",
         "--postprocessor-args", "-ac 1", "-o", output_path, episode_url],
        check=True,
    )


# ============================================================
# 语音转文字
# ============================================================
def transcribe(audio_path):
    print("  加载 Whisper 模型...")
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="cpu", compute_type="int8")
    print("  转录中...")
    segments, info = model.transcribe(
        audio_path,
        language="zh",
        beam_size=5,
        initial_prompt="以下是一档中文读书播客节目的内容。",
        condition_on_previous_text=False,  # 防止音乐段触发循环幻觉
        no_speech_threshold=0.6,           # 跳过纯音乐/静音段
    )
    transcript = "".join(seg.text for seg in segments)
    print(f"  转录完成，共 {len(transcript)} 字")
    return transcript


# ============================================================
# AI 总结
# ============================================================
def summarize(transcript, episode_title, need_quotes=True, need_booklist=True):
    content = transcript[:12000]

    quotes_field = '"金句": ["金句1", "金句2", "金句3"],' if need_quotes else '"金句": [],'
    booklist_field = '"书单": ["文中提到的所有书名，每本单独一条"]' if need_booklist else '"书单": []'

    prompt = f"""这是一档读书类播客的完整文字稿，本集标题是：{episode_title}

请仔细阅读，以 JSON 格式返回分析结果。只返回 JSON，不要任何解释，不要 markdown 代码块。

{{
  "拆解书名": "本集重点拆解的书名",
  "核心认知": ["认知点1", "认知点2", "认知点3", "认知点4", "认知点5"],
  {quotes_field}
  {booklist_field},
  "行动建议": ["可执行建议1", "建议2"]
}}

文字稿：
{content}"""

    for model_name in MODELS:
        try:
            print(f"  尝试模型：{model_name}")
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/podcast-auto",
                },
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1500,
                },
                timeout=120,
            )
            resp_json = resp.json()
            print(f"  API 响应：{json.dumps(resp_json, ensure_ascii=False)[:400]}")
            if "choices" not in resp_json:
                raise ValueError(f"无 choices：{resp_json}")
            raw = resp_json["choices"][0]["message"]["content"]
            raw = raw.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            print(f"  总结成功（{model_name}）")
            return result
        except Exception as e:
            print(f"  {model_name} 失败：{e}")
            time.sleep(5)

    print("  所有模型失败，返回空摘要")
    return {}


# ============================================================
# 处理单集
# ============================================================
def process_episode(episode):
    episode_url   = episode.get("link", "")
    episode_title = episode.get("title", "未知标题")
    episode_date  = episode.get("published", "")

    print(f"\n  处理：{episode_title}")
    print(f"  链接：{episode_url}")

    # 1. 从 RSS 提取金句和书单
    print("\n📋 解析 RSS 简介...")
    highlights, rss_booklist = extract_from_description(episode)

    # 2. 下载 + 转录
    print("\n⬇️  下载音频...")
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "episode.mp3")
        download_audio(episode_url, audio_path)
        print("\n🎙️  转录音频...")
        transcript = transcribe(audio_path)

    # 3. AI 总结（RSS 已有的字段不让 AI 重复提取，节省 token）
    print("\n🤖 AI 总结...")
    summary = summarize(
        transcript, episode_title,
        need_quotes=(not highlights),
        need_booklist=(not rss_booklist),
    )

    # 4. 合并：RSS 有就用 RSS 的，没有用 AI 提取的
    final_quotes   = highlights   or "\n".join(f"• {x}" for x in summary.get("金句", []))
    final_booklist = rss_booklist or "\n".join(f"• {x}" for x in summary.get("书单", []))

    # 5. 写入飞书
    print("\n📝 写入飞书...")
    fields = {
        "拆解书名": summary.get("拆解书名", "（待解析）"),
        "标题":     episode_title,
        "发布日期": episode_date,
        "原链接":   episode_url,
        "核心认知": "\n".join(f"• {x}" for x in summary.get("核心认知", [])),
        "金句":     final_quotes,
        "书单":     final_booklist,
        "完整转录": transcript[:50000],
        "处理状态": "已完成",
    }
    result = write_to_feishu(fields)
    record_id = result.get("data", {}).get("record", {}).get("record_id", "未知")
    print(f"  记录 ID：{record_id}")

    # 6. 飞书通知
    print("\n🔔 发送飞书通知...")
    send_feishu_notification(episode_title, episode_date, episode_url)

    return True


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("🎙️ 播客精华提取器启动")
    print("=" * 50)

    print("\n📡 拉取 RSS...")
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("RSS 无内容，退出")
        return
    print(f"  共获取 {len(feed.entries)} 集")

    print("\n🔍 飞书去重检查...")
    existing_links = get_existing_links()
    print(f"  飞书已有 {len(existing_links)} 条记录")

    unprocessed = [
        ep for ep in feed.entries
        if ep.get("link", "") not in existing_links
    ]

    if not unprocessed:
        print("\n✅ 所有集均已处理，无需操作")
        return

    print(f"  共 {len(unprocessed)} 集未处理")

    # 有新集优先处理新集，否则从最近的未处理集往前回填
    latest_ep = feed.entries[0]
    if latest_ep.get("link", "") not in existing_links:
        target = latest_ep
        print("\n🆕 发现新集，优先处理最新集")
    else:
        target = unprocessed[0]  # 最近的未处理集
        print("\n📚 无新集，回填历史集")

    process_episode(target)
    print("\n✅ 本次运行完成！")


if __name__ == "__main__":
    main()
