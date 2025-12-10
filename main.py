from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, PlainTextResponse
import re
import httpx
from urllib.parse import urlparse, parse_qs
import asyncio
from cachetools import TTLCache
from datetime import datetime
import time

app = FastAPI()

# ===== 缓存配置 =====
# 最多缓存1000个视频链接，每个链接有独立的过期时间
video_cache = TTLCache(maxsize=1000, ttl=3600)  # 默认TTL 1小时，实际会根据deadline动态调整

# ===== 工具函数：av 转 bv =====
XOR_CODE = 23442827791579
MAX_AID = 1 << 51
BASE = 58
data = 'FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf'

def av2bv(av: str) -> str:
    aid_str = av[2:] if av.lower().startswith('av') else av
    try:
        aid = int(aid_str)
    except ValueError:
        raise ValueError("Invalid av number")
    tmp = (MAX_AID | aid) ^ XOR_CODE
    bytes_list = ['B', 'V', '1', '0', '0', '0', '0', '0', '0', '0', '0', '0']
    bv_index = len(bytes_list) - 1
    while tmp > 0:
        bytes_list[bv_index] = data[tmp % BASE]
        tmp //= BASE
        bv_index -= 1
    # Swap positions
    bytes_list[3], bytes_list[9] = bytes_list[9], bytes_list[3]
    bytes_list[4], bytes_list[7] = bytes_list[7], bytes_list[4]
    return ''.join(bytes_list)

# ===== 辅助函数：从URL中提取deadline并计算过期时间 =====
def extract_deadline(url: str) -> int:
    """从视频直链中提取deadline参数，返回剩余秒数"""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if 'deadline' in params:
            deadline_timestamp = int(params['deadline'][0])
            current_timestamp = int(time.time())
            # 返回剩余秒数，如果已过期返回0
            remaining = max(0, deadline_timestamp - current_timestamp)
            return remaining
    except Exception:
        pass
    # 如果无法提取deadline，返回默认1小时
    return 3600

# ===== 核心解析函数 =====
async def parse_bilibili_video(url: str) -> str:
    # 提取 BV 或 AV
    bv_id = None
    if "BV" in url:
        match = re.search(r'(?i)BV[0-9A-Za-z]+', url)
        if match:
            bv_id = match.group(0)
    elif "av" in url.lower():
        match = re.search(r'(?i)av\d+', url)
        if match:
            bv_id = av2bv(match.group(0))
    
    if not bv_id:
        raise ValueError("无法提取视频编号")

    # 提取分P参数（p值，默认为1）
    p_num = 1
    parsed_url = urlparse(url)
    url_params = parse_qs(parsed_url.query)
    if 'p' in url_params:
        try:
            p_num = int(url_params['p'][0])
        except (ValueError, IndexError):
            p_num = 1

    # 检查缓存（缓存键包含bv号和p值）
    cache_key = f"bilibili_{bv_id}_p{p_num}"
    if cache_key in video_cache:
        cached_url = video_cache[cache_key]
        # 验证缓存的URL是否还有效（检查deadline）
        remaining_time = extract_deadline(cached_url)
        if remaining_time > 60:  # 至少还有60秒有效期才使用缓存
            print(f"[缓存命中] {bv_id} P{p_num}, 剩余有效时间: {remaining_time}秒")
            return cached_url
        else:
            # 缓存即将过期，删除缓存
            print(f"[缓存过期] {bv_id} P{p_num}, 重新获取")
            del video_cache[cache_key]

    # 获取 cid
    async with httpx.AsyncClient() as client:
        info_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = await client.get(info_url, headers=headers)
        resp.raise_for_status()
        video_data = resp.json()
        
        # 获取对应分P的cid
        pages = video_data.get("data", {}).get("pages", [])
        if not pages:
            raise ValueError("获取视频分P信息失败")
        
        # 根据p值获取对应的cid（p值从1开始，数组索引从0开始）
        page_index = p_num - 1
        if page_index < 0 or page_index >= len(pages):
            raise ValueError(f"无效的分P参数: p={p_num}，视频共有{len(pages)}个分P")
        
        cid = pages[page_index].get("cid")
        if not cid:
            raise ValueError("获取 cid 失败")

        # 获取播放地址（qn=116 为 1080P+）
        play_url = (
            f"https://api.bilibili.com/x/player/playurl"
            f"?bvid={bv_id}&cid={cid}&qn=116&otype=json&platform=html5&high_quality=1"
        )
        play_resp = await client.get(play_url, headers=headers)
        play_resp.raise_for_status()
        play_data = play_resp.json()
        durl_list = play_data.get("data", {}).get("durl")
        if not durl_list or not durl_list[0].get("url"):
            raise ValueError("无法获取视频直链")
        
        direct_url = durl_list[0]["url"]
        
        # 将结果存入缓存
        # 计算实际的过期时间（比deadline提前5分钟，确保不会返回已过期的链接）
        ttl = extract_deadline(direct_url) - 300  # 提前5分钟过期
        if ttl > 0:
            # 手动设置带过期时间的缓存
            video_cache[cache_key] = direct_url
            print(f"[缓存存储] {bv_id} P{p_num}, TTL: {ttl}秒")
        
        return direct_url

# ===== 主路由 =====
@app.get("/proxy")
async def proxy(request: Request):
    url_param = request.query_params.get("url")
    if not url_param:
        return PlainTextResponse("Error: Missing URL parameter", status_code=400)

    # 兼容处理：如果URL参数被截断，尝试重建完整URL
    # 检查是否有额外的查询参数（可能是从原始URL中分离出来的）
    extra_params = {}
    for key, value in request.query_params.items():
        if key not in ['url']:  # 排除url参数本身
            extra_params[key] = value
    
    # 如果有额外参数且URL是B站链接，重建完整URL
    if extra_params and "bilibili.com" in url_param:
        from urllib.parse import urlencode
        extra_query = urlencode(extra_params)
        # 判断原URL是否已有查询参数
        if '?' in url_param:
            url_param = f"{url_param}&{extra_query}"
        else:
            url_param = f"{url_param}?{extra_query}"
        print(f"[URL重建] 重建后的完整URL: {url_param}")

    parsed = urlparse(url_param)
    if not parsed.scheme or not parsed.netloc:
        return PlainTextResponse("Error: Invalid URL", status_code=400)

    try:
        if "bilibili.com" in url_param:
            final_url = await parse_bilibili_video(url_param)
            return RedirectResponse(url=final_url, status_code=302)
        else:
            # 其他平台：抓取 HTML 并提取 playAddr
            async with httpx.AsyncClient() as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                resp = await client.get(url_param, headers=headers)
                resp.raise_for_status()
                body_text = resp.text

                match = re.search(r'"playAddr":\{.*?\}', body_text, re.DOTALL)
                if match:
                    json_str = "{" + match.group(0) + "}"
                    try:
                        import json
                        play_addr = json.loads(json_str)
                        video_url = play_addr.get("playAddr", {}).get("ori_m3u8")
                        if video_url:
                            return RedirectResponse(url=video_url, status_code=302)
                        else:
                            return PlainTextResponse("Error: Video URL not found in playAddr", status_code=404)
                    except Exception:
                        return PlainTextResponse("Error: Failed to parse playAddr JSON", status_code=400)
                else:
                    return PlainTextResponse("Error: No playAddr found in HTML", status_code=404)

    except Exception as e:
        return PlainTextResponse(f"An error occurred: {str(e)}", status_code=500)

# ===== 启动入口（可选）=====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)