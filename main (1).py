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

FEISHU_APP_ID     = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FEISHU_APP_TOKEN  = os.environ["FEISHU_APP_TOKEN"]
FEISHU_TABLE_ID   = os.environ["FEISHU_TABLE_ID"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# 三重备用模型，依次尝试
MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "qwen/qwen3-4b:free",
    "openrouter/free",
]

# ============================================================
# 飞书 API
# ============================================================
def get_feishu_token():
    """获取飞书访问令牌"""
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()["tenant_access_token"]


def get_existing_links(token):
    """查询飞书表格中已有的链接，用于去重"""
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/"
        f"apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
    )
    resp = requests.get(url, headers=headers, params={"page_size": 100}, timeout=10)
    records = resp.json().get("data", {}).get("items", [])
    return {r["fields"].get("原链接", "") for r in records}


def write_to_feishu(token, fields):
    """向飞书表格写入一条记录"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/"
        f"apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
    )
    resp = requests.post(url, headers=headers, json={"fields": fields}, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# 音频下载
# ============================================================
def download_audio(episode_url, output_path):
    """用 yt-dlp 下载音频并转为低码率 MP3"""
    print(f"  下载音频：{episode_url}")
    subprocess.run(
        [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "32K",   # 32kbps 单声道，85分钟约 20MB
            "--postprocessor-args", "-ac 1",  # 强制单声道
            "-o", output_path,
            episode_url,
        ],
        check=True,
    )


# ============================================================
# 语音转文字
# ============================================================
def transcribe(audio_path):
    """使用 Faster-Whisper large-v3 转录中文音频"""
    print("  加载 Whisper 模型（首次运行会下载约 3GB，后续从缓存读取）...")
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="cpu", compute_type="int8")

    print("  转录中，请耐心等待（约 30-60 分钟）...")
    segments, info = model.transcribe(
        audio_path,
        language="zh",
        beam_size=5,
        initial_prompt="以下是一档中文读书播客节目的内容。",
    )
    transcript = "".join(seg.text for seg in segments)
    print(f"  转录完成，共 {len(transcript)} 字")
    return transcript


# ============================================================
# AI 总结
# ============================================================
def summarize(transcript, episode_title):
    """调用 OpenRouter 总结，三重模型备用"""

    # 截取前 15000 字（约够 85 分钟播客的核心内容）
    content = transcript[:15000]

    prompt = f"""这是一档读书类播客的完整文字稿，本集标题是：{episode_title}

请仔细阅读文字稿，以 JSON 格式返回以下内容。
注意：只返回 JSON，不要任何解释文字，不要 markdown 代码块。

{{
  "拆解书名": "本集拆解的书名（如果标题里没有，从内容推断）",
  "核心认知": [
    "认知点1（一句话概括，有具体信息）",
    "认知点2",
    "认知点3",
    "认知点4",
    "认知点5"
  ],
  "金句": [
    "值得记录的金句或观点1",
    "金句2",
    "金句3"
  ],
  "书单": [
    "节目中提到的其他书名1",
    "书名2"
  ],
  "行动建议": [
    "对听众的具体行动建议1",
    "建议2"
  ]
}}

文字稿内容：
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
            raw = resp.json()["choices"][0]["message"]["content"]
            # 清理可能的 markdown 代码块
            raw = raw.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            print(f"  总结成功（使用 {model_name}）")
            return result
        except Exception as e:
            print(f"  模型 {model_name} 失败：{e}，尝试下一个...")
            time.sleep(3)

    print("  所有模型均失败，返回空摘要")
    return {}


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("🎙️ 播客精华提取器启动")
    print("=" * 50)

    # 1. 拉取 RSS
    print("\n📡 检查 RSS 更新...")
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("RSS 无内容或暂时不可用，退出")
        return

    latest = feed.entries[0]
    episode_url   = latest.get("link", "")
    episode_title = latest.get("title", "未知标题")
    episode_date  = latest.get("published", "")

    print(f"  最新一集：{episode_title}")
    print(f"  链接：{episode_url}")

    # 2. 飞书去重检查
    print("\n🔍 检查是否已处理过...")
    feishu_token = get_feishu_token()
    existing_links = get_existing_links(feishu_token)

    if episode_url in existing_links:
        print("  该集已处理过，无需重复处理，退出")
        return

    print("  新内容，开始处理！")

    # 3. 下载 + 转录（在临时目录操作，完成后自动清理）
    print("\n⬇️  下载音频...")
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "episode.mp3")
        download_audio(episode_url, audio_path)

        print("\n🎙️  转录音频...")
        transcript = transcribe(audio_path)
    # 临时目录在此处自动删除，释放磁盘空间

    # 4. AI 总结
    print("\n🤖 AI 总结...")
    summary = summarize(transcript, episode_title)

    # 5. 写入飞书
    print("\n📝 写入飞书多维表格...")
    fields = {
        "拆解书名": summary.get("拆解书名", ""),
        "标题":     episode_title,
        "发布日期": episode_date,
        "原链接":   episode_url,
        "核心认知": "\n".join(f"• {x}" for x in summary.get("核心认知", [])),
        "金句":     "\n".join(f"• {x}" for x in summary.get("金句", [])),
        "书单":     "\n".join(f"• {x}" for x in summary.get("书单", [])),
        "完整转录": transcript,
        "处理状态": "已完成",
    }

    result = write_to_feishu(feishu_token, fields)
    print(f"  写入结果：{result.get('code')} {result.get('msg', '')}")

    print("\n✅ 全部完成！")


if __name__ == "__main__":
    main()
