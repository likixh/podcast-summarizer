import os
import json
import time
import feedparser
import requests
import subprocess
import tempfile

# ============================================================
# 配置
# ============================================================
RSS_URL = "https://www.ximalaya.com/album/80074602.xml"

FEISHU_APP_ID      = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET  = os.environ["FEISHU_APP_SECRET"]
FEISHU_APP_TOKEN   = os.environ["FEISHU_APP_TOKEN"]
FEISHU_TABLE_ID    = os.environ["FEISHU_TABLE_ID"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "qwen/qwen3-4b:free",
    "openrouter/free",
]

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


def get_existing_links(token):
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/"
        f"apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
    )
    resp = requests.get(url, headers=headers, params={"page_size": 100}, timeout=10)
    data = resp.json()
    print(f"  查询记录：code={data.get('code')} msg={data.get('msg')}")
    records = data.get("data", {}).get("items", [])
    return {r["fields"].get("原链接", "") for r in records}


def write_to_feishu(token, fields):
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
        audio_path, language="zh", beam_size=5,
        initial_prompt="以下是一档中文读书播客节目的内容。",
    )
    transcript = "".join(seg.text for seg in segments)
    print(f"  转录完成，共 {len(transcript)} 字")
    return transcript


# ============================================================
# AI 总结
# ============================================================
def summarize(transcript, episode_title):
    content = transcript[:12000]
    prompt = f"""这是一档读书类播客的完整文字稿，本集标题是：{episode_title}

请仔细阅读，以 JSON 格式返回。只返回 JSON，不要任何解释，不要 markdown 代码块。

{{
  "拆解书名": "本集拆解的书名",
  "核心认知": ["认知点1", "认知点2", "认知点3", "认知点4", "认知点5"],
  "金句": ["金句1", "金句2", "金句3"],
  "书单": ["提到的其他书名1", "书名2"],
  "行动建议": ["行动建议1", "建议2"]
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
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("🎙️ 播客精华提取器启动")
    print("=" * 50)

    print("\n📡 检查 RSS...")
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("RSS 无内容，退出")
        return

    latest        = feed.entries[0]
    episode_url   = latest.get("link", "")
    episode_title = latest.get("title", "未知标题")
    episode_date  = latest.get("published", "")
    print(f"  最新一集：{episode_title}")
    print(f"  链接：{episode_url}")

    print("\n🔍 飞书去重检查...")
    feishu_token   = get_feishu_token()
    existing_links = get_existing_links(feishu_token)

    if episode_url in existing_links:
        print("  已处理过，退出")
        return

    print("  新内容，开始处理！")

    print("\n⬇️  下载音频...")
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "episode.mp3")
        download_audio(episode_url, audio_path)
        print("\n🎙️  转录音频...")
        transcript = transcribe(audio_path)

    print("\n🤖 AI 总结...")
    summary = summarize(transcript, episode_title)

    print("\n📝 写入飞书...")
    fields = {
        "拆解书名": summary.get("拆解书名", "（待解析）"),
        "标题":     episode_title,
        "发布日期": episode_date,
        "原链接":   episode_url,
        "核心认知": "\n".join(f"• {x}" for x in summary.get("核心认知", [])),
        "金句":     "\n".join(f"• {x}" for x in summary.get("金句", [])),
        "书单":     "\n".join(f"• {x}" for x in summary.get("书单", [])),
        "完整转录": transcript[:50000],
        "处理状态": "已完成",
    }

    result = write_to_feishu(feishu_token, fields)
    record_id = result.get("data", {}).get("record", {}).get("record_id", "未知")
    print(f"\n✅ 完成！记录 ID：{record_id}")


if __name__ == "__main__":
    main()
