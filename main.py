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
    "deepseek/deepseek-r1-0528:free",
    "qwen/qwen3-235b-a22b:free",
    "openrouter/free",
]

# ============================================================
# 从 RSS 简介提取所有信息
# ============================================================
def extract_from_description(entry, episode_title):
    description = ""
    if hasattr(entry, "summary"):
        description = entry.summary
    elif hasattr(entry, "content") and entry.content:
        description = entry.content[0].get("value", "")

    if not description:
        return "", "", "", ""

    # 清理 HTML
    description = re.sub(r"<[^>]+>", "\n", description)
    description = re.sub(r"&nbsp;", " ", description)
    description = re.sub(r"&amp;", "&", description)
    description = re.sub(r"&#\d+;", "", description)
    description = re.sub(r"\r\n", "\n", description)
    description = re.sub(r"\n{3,}", "\n\n", description)

    print(f"  RSS 简介已解析：{len(description)} 字")

    # ── 1. 拆解书名：优先从标题提取《书名》，否则从简介第一个《》提取 ──
    book_name = ""
    title_match = re.search(r"《([^》]+)》", episode_title)
    if title_match:
        book_name = title_match.group(1)
    else:
        # 标题里没有书名号，用竖线前的关键词
        title_core = re.sub(r"^\d+\s*", "", episode_title)  # 去掉集数
        title_core = title_core.split("｜")[0].split("|")[0].strip()
        book_name = title_core
    print(f"  ✅ 拆解书名：已提取")

    # ── 2. Highlights：匹配 Highlights 到第一个时间戳之间的内容 ──
    highlights = ""
    # 兼容：Highlights、Highlights（*为引用）、highlights: 等变体
    match = re.search(
        r"[Hh]ighlights[^\\n]*\n+([\s\S]+?)(?=\n\d{1,3}:\d{2}|\Z)",
        description
    )
    if match:
        raw = match.group(1).strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        # 去掉行首的各种符号（·、•、-、*等）
        cleaned = [re.sub(r"^[·•\-\*\uff65]+\s*", "", l) for l in lines]
        # 去掉行尾的 * 标注
        cleaned = [l.rstrip("*").strip() for l in cleaned if l]
        highlights = "\n".join(f"• {l}" for l in cleaned)
        print(f"  ✅ 提取到 Highlights：{len(cleaned)} 条")
    else:
        print("  ℹ️  未找到 Highlights")

    # ── 3. 核心认知：从时间戳列表直接提取，格式 "00:04:20 内容" ──
    core_insights = ""
    timestamp_matches = re.findall(
        r"\n\d{1,3}:\d{2}(?::\d{2})?\s+(.+)", description
    )
    if timestamp_matches:
        # 过滤太短的（少于8字可能是噪音）
        valid = [t.strip() for t in timestamp_matches if len(t.strip()) >= 8]
        core_insights = "\n".join(f"• {t}" for t in valid)
        print(f"  ✅ 从时间戳提取核心认知：{len(valid)} 条")
    else:
        print("  ℹ️  未找到时间戳，核心认知将由 AI 提取")

    # ── 4. 书单：从全文提取所有《书名》，排除拆解书名本身 ──
    all_books = re.findall(r"《([^》]+)》", description)
    # 去重，排除主讲书
    unique_books = list(dict.fromkeys(
        b for b in all_books if b != book_name
    ))
    booklist = "\n".join(f"• 《{b}》" for b in unique_books)
    if booklist:
        print(f"  ✅ 提取到书单：{len(unique_books)} 本")
    else:
        print("  ℹ️  未找到其他书单，将由 AI 提取")

    return book_name, highlights, core_insights, booklist


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
    print(f"  写入字段数：{len(fields)}")
    resp = requests.post(url, headers=headers, json={"fields": fields}, timeout=30)
    data = resp.json()
    print(f"  飞书写入：status={resp.status_code} code={data.get('code')} msg={data.get('msg')}")
    resp.raise_for_status()
    return data


def send_feishu_notification(title, date, link):
    msg = {
        "msg_type": "interactive",
        "card": {
            "elements": [{
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
            }],
            "header": {
                "title": {"tag": "plain_text", "content": "🎙️ 自习室播客精华更新"},
                "template": "blue"
            }
        }
    }
    resp = requests.post(FEISHU_WEBHOOK, json=msg, timeout=10)
    try:
        data = resp.json()
        print(f"  机器人通知：status={resp.status_code} code={data.get('code')} msg={data.get('msg')}")
    except ValueError:
        print(f"  机器人通知：status={resp.status_code}")


# ============================================================
# 音频下载
# ============================================================
def download_audio(episode_url, output_path):
    print("  下载音频：开始")
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
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    transcript = "".join(seg.text for seg in segments)
    print(f"  转录完成，共 {len(transcript)} 字")
    return transcript


# ============================================================
# AI 总结（仅用于补充 RSS 没有的字段）
# ============================================================
def summarize_with_ai(transcript, episode_title, need_quotes, need_booklist, need_insights):
    """只在 RSS 提取不到时才调用 AI"""
    if not need_quotes and not need_booklist and not need_insights:
        print("  RSS 已提供全部字段，跳过 AI 总结")
        return {}

    content = transcript[:12000]
    fields_needed = []
    json_template = {}

    if need_insights:
        fields_needed.append("核心认知")
        json_template["核心认知"] = ["认知点1", "认知点2", "认知点3", "认知点4", "认知点5"]
    if need_quotes:
        fields_needed.append("金句")
        json_template["金句"] = ["金句1", "金句2", "金句3"]
    if need_booklist:
        fields_needed.append("书单")
        json_template["书单"] = ["书名1", "书名2"]

    prompt = f"""这是一档读书类播客的完整文字稿，本集标题是：{episode_title}

请提取以下字段：{', '.join(fields_needed)}
只返回 JSON，不要任何解释，不要 markdown 代码块。

{json.dumps(json_template, ensure_ascii=False, indent=2)}

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
            print(f"  API 响应：status={resp.status_code} has_choices={'choices' in resp_json}")
            if "choices" not in resp_json:
                raise ValueError("无 choices")

            # 处理部分模型返回 content=null 但有 reasoning 的情况
            message = resp_json["choices"][0]["message"]
            raw = message.get("content") or message.get("reasoning") or ""
            if not raw:
                raise ValueError("content 和 reasoning 均为空")

            raw = raw.strip().replace("```json", "").replace("```", "").strip()
            # 找到第一个 { 开始解析，跳过可能的前缀文字
            json_start = raw.find("{")
            if json_start > 0:
                raw = raw[json_start:]
            result = json.loads(raw)
            print(f"  AI 总结成功（{model_name}）")
            return result
        except Exception as e:
            print(f"  {model_name} 失败：{type(e).__name__}")
            time.sleep(5)

    print("  所有模型失败，返回空")
    return {}


# ============================================================
# 处理单集
# ============================================================
def process_episode(episode):
    episode_url   = episode.get("link", "")
    episode_title = episode.get("title", "未知标题")
    episode_date  = episode.get("published", "")

    print("\n  处理单集：开始")

    # 1. 从 RSS 提取所有字段
    print("\n📋 解析 RSS 简介...")
    book_name, highlights, core_insights, booklist = extract_from_description(
        episode, episode_title
    )

    # 2. 下载 + 转录
    print("\n⬇️  下载音频...")
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "episode.mp3")
        download_audio(episode_url, audio_path)
        print("\n🎙️  转录音频...")
        transcript = transcribe(audio_path)

    # 3. AI 补充（只补 RSS 缺失的字段）
    print("\n🤖 AI 补充缺失字段...")
    ai_result = summarize_with_ai(
        transcript, episode_title,
        need_quotes=(not highlights),
        need_booklist=(not booklist),
        need_insights=(not core_insights),
    )

    # 4. 合并：RSS 优先，AI 兜底
    final_book     = book_name     or ai_result.get("拆解书名", "（待解析）")
    final_insights = core_insights or "\n".join(f"• {x}" for x in ai_result.get("核心认知", []))
    final_quotes   = highlights    or "\n".join(f"• {x}" for x in ai_result.get("金句", []))
    final_booklist = booklist      or "\n".join(f"• {x}" for x in ai_result.get("书单", []))

    # 5. 写入飞书
    print("\n📝 写入飞书...")
    fields = {
        "拆解书名": final_book,
        "标题":     episode_title,
        "发布日期": episode_date,
        "原链接":   episode_url,
        "核心认知": final_insights,
        "金句":     final_quotes,
        "书单":     final_booklist,
        "完整转录": transcript[:50000],
        "处理状态": "已完成",
    }
    result = write_to_feishu(fields)
    record_id = result.get("data", {}).get("record", {}).get("record_id", "未知")
    print(f"  写入结果：record_id_present={record_id != '未知'}")

    # 6. 飞书通知
    print("\n🔔 发送飞书通知...")
    send_feishu_notification(episode_title, episode_date, episode_url)


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

    latest_ep = feed.entries[0]
    if latest_ep.get("link", "") not in existing_links:
        target = latest_ep
        print("\n🆕 发现新集，优先处理最新集")
    else:
        target = unprocessed[0]
        print("\n📚 无新集，回填历史集")

    process_episode(target)
    print("\n✅ 本次运行完成！")


if __name__ == "__main__":
    main()
