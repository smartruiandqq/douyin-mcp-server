import os
import re
import json
import requests
import tempfile
import sys

import asyncio
from functools import partial
from typing import Any


from pathlib import Path
from typing import Optional, Tuple
import ffmpeg

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Context




mcp = FastMCP("Douyin MCP Server")

# 请求头，模拟移动端访问
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1'
}

# 默认 API 配置
DEFAULT_API_BASE_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"

class Logger:
    @staticmethod
    def log(message):
        sys.stderr.write(f"{message}\n")  # ✅ 客户端可捕获
        
class DouyinProcessor:
    """抖音视频处理器"""
    def __init__(self, api_key: str, api_base_url: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key
        self.api_base_url = api_base_url or DEFAULT_API_BASE_URL
        self.model = model or DEFAULT_MODEL
        self.temp_dir = Path(tempfile.mkdtemp(prefix="dy_"))
    
    def __del__(self):
        import shutil
        if hasattr(self, "temp_dir") and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def parse_share_url(self, share_text: str) -> dict:
        """从分享文本中提取无水印视频链接"""
        # 提取分享链接
        urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', share_text)
        if not urls:
            raise ValueError("未找到有效的分享链接")
        
        share_url = urls[0]
        #获取302跳转地址，类似https://www.iesdouyin.com/share/video/7515390498845035830
        share_response = requests.get(share_url, headers=HEADERS)
        share_response.raise_for_status()
        video_id = share_response.url.split("?")[0].strip("/").split("/")[-1]
        share_url  = f'https://www.iesdouyin.com/share/video/{video_id}/'

        # 获取视频页面内容，一定要模拟移动端访问
        response = requests.get(share_url, headers=HEADERS)
        response.raise_for_status()

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        find_res = pattern.search(response.text)

        if not find_res or not find_res.group(1):
            raise ValueError("从HTML中解析视频信息失败")

         # 解析JSON数据
        json_data = json.loads(find_res.group(1).strip())
        VIDEO_ID_PAGE_KEY = "video_(id)/page"
        NOTE_ID_PAGE_KEY = "note_(id)/page"

        if VIDEO_ID_PAGE_KEY in json_data["loaderData"]:
            original_video_info = json_data["loaderData"][VIDEO_ID_PAGE_KEY]["videoInfoRes"]
        elif NOTE_ID_PAGE_KEY in json_data["loaderData"]:
            original_video_info = json_data["loaderData"][NOTE_ID_PAGE_KEY]["videoInfoRes"]
        else:
            raise Exception("无法从JSON中解析视频或图集信息")

        data = original_video_info["item_list"][0]

        # 获取视频信息
        video_url = data["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
        cover_url = data["video"]["cover"]["url_list"][0]
        desc = data.get("desc", "").strip() or f"douyin_{video_id}"
        
        # 替换文件名中的非法字符
        desc = re.sub(r'[\\/:*?"<>|]', '_', desc)
        
        return {
            "url": video_url,
            "title": desc,
            "video_id": video_id,
            "cover":cover_url
        }

    


    # --- 辅助函数：包含所有阻塞的 requests 代码 ---
    # 这个函数将在一个单独的线程中被执行。
    def _blocking_download_worker(self, ctx: Any, url: str, local_path: str):
        """
        这是一个同步的、阻塞的下载函数，设计为在线程池中运行。
        它使用 requests 进行流式下载并报告进度。
        
        Args:
            ctx: mcp 上下文对象。
            url (str): 文件的 URL。
            local_path (str): 本地保存路径。
        
        Raises:
            requests.exceptions.RequestException: 如果下载过程中发生网络错误。
        """
        try:
            # 使用 stream=True 进行流式下载，避免一次性将大文件读入内存
            with requests.get(url, stream=True, timeout=30) as response:
                # 如果服务器返回错误状态码 (如 404, 500), 则抛出异常
                response.raise_for_status()

                # 从响应头获取文件总大小，用于计算进度
                total_size_str = response.headers.get('content-length')
                if total_size_str is None:
                    total_size = 0
                    print("警告: 响应头中未找到 'content-length'，无法显示下载进度。")
                else:
                    total_size = int(total_size_str)

                downloaded_size = 0
                filename = os.path.basename(local_path)
                
                # 以二进制写入模式打开本地文件
                with open(local_path, 'wb') as f:
                    # 迭代下载内容块，chunk_size 可以根据网络情况调整
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        # 如果有总大小，则计算并报告进度
                        if total_size > 0:
                            progress = downloaded_size / total_size
                            # 从工作线程调用 ctx.report_progress
                            ctx.report_progress(progress)

            # 确保下载完成后，进度条显示为100%
            if total_size > 0:
               ctx.report_progress(1.0)
            return local_path

        except requests.exceptions.RequestException as e:
            # 最好在这里重新抛出异常，这样主异步函数就能捕获到它
            raise e

    # --- 主要的异步接口函数 ---
    # 这是您在代码中应该调用的函数。
    async def download_mp4_async(self, video_info: dict , ctx: Context) -> Path:
        """
        异步下载 MP4 文件到本地临时目录。
        """

        filename = f"{video_info['title']}.mp4"
        local_path = self.temp_dir / filename
        
        url = video_info['url']
        Logger.log(f"任务启动: 准备下载 '{url}'\n -> 保存至: '{local_path}'")
        # 获取当前正在运行的 asyncio 事件循环
        loop = asyncio.get_running_loop()

        # 使用 functools.partial 将参数预先绑定到我们的工作函数中，
        # 因为 run_in_executor 需要一个不带参数的可调用对象。
        download_task = partial(self._blocking_download_worker, ctx, video_info["url"], local_path)

        try:
            # 在默认的线程池执行器中运行阻塞的下载任务。
            # 'await' 会在此处暂停，直到线程中的任务完成或抛出异常。
            # 事件循环在此期间可以自由处理其他异步任务。
            await loop.run_in_executor(None, download_task)
        except Exception as e:
            print(f"\n下载任务在后台线程中失败: {e}")
            # 将异常继续向上抛出，让调用者可以处理
            raise e
        
        print(f"文件下载任务已成功在后台线程完成。")
        return local_path

    def extract_audio(self, video_path: Path) -> Path:
        """从视频文件中提取音频"""
        audio_path = video_path.with_suffix('.mp3')
        Logger.log(f"audio_path:{audio_path}")
        
        try:
            (
                ffmpeg
                .input(str(video_path))
                .output(str(audio_path), acodec='libmp3lame', q=0)
                .run(capture_stdout=True, capture_stderr=True, overwrite_output=True)
            )
            return audio_path
        except Exception as e:
            Logger.log(f"提取音频时出错:{audio_path}")
            raise Exception(f"提取音频时出错: {str(e)}")
    
    def extract_text_from_audio(self, audio_path: Path) -> str:
        """从音频文件中提取文本"""
        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }

        try:
            with open(audio_path, "rb") as file:
                files = {
                    "file": (audio_path.name, file, "audio/mpeg"),
                    "model": (None, self.model)
                }

                response = requests.post(self.api_base_url, headers=headers, files=files)

                if response.status_code == 200:
                    return response.json().get("text", "")
                else:
                    raise Exception(f"API请求失败: {response.status_code}, {response.text}")

        except Exception as e:
            print(f"语音转文本过程中发生错误: {str(e)}")
            raise    

    def cleanup_files(self, *file_paths: Path):
        """清理指定的文件"""
        for file_path in file_paths:
            if file_path.exists():
                file_path.unlink()


@mcp.tool()
async def extract_douyin_text(
    share_link: str,
    api_base_url: Optional[str] = None,
    model: Optional[str] = None,
    ctx: Context = None
) -> str:
    """
    从抖音分享链接提取视频中的文本内容
    
    参数:
    - share_link: 抖音分享链接或包含链接的文本
    - api_base_url: API基础URL（可选，默认使用SiliconFlow）
    - model: 语音识别模型（可选，默认使用SenseVoiceSmall）
    
    返回:
    - 提取的文本内容
    
    注意: 需要设置环境变量 DOUYIN_API_KEY
    """
    try:
        # 从环境变量获取API密钥
        api_key = os.getenv('DOUYIN_API_KEY')
        if not api_key:
            raise ValueError("未设置环境变量 DOUYIN_API_KEY，请在配置中添加语音识别API密钥")
        
        processor = DouyinProcessor(api_key, api_base_url, model)
        
        # 解析视频链接
        ctx.info("正在解析抖音分享链接...")
        video_info = processor.parse_share_url(share_link)
       

         # 下载视频
        ctx.info("正在下载视频...")
        video_path = await processor.download_mp4_async(video_info, ctx)
        Logger.log(video_path)
        # 提取音频
        Logger.log("正在提取音频...")
        audio_path = processor.extract_audio(video_path)

        # 提取文本
        ctx.info("正在从音频中提取文本...")
        text_content = processor.extract_text_from_audio(audio_path)
        
        # 清理临时文件
        ctx.info("正在清理临时文件...")
        processor.cleanup_files(video_path, audio_path)
        
        ctx.info("文本提取完成!")
        return text_content

        
        
    except Exception as e:
        ctx.error(f"处理过程中出现错误: {str(e)}")
        raise Exception(f"提取抖音视频文本失败: {str(e)}")




@mcp.tool()
def get_douyin_download_link(share_link : str) -> str:
    """获取抖音视频无水印下载链接
    参数：
    - share_link: 抖音视频分享链接
    返回值：
    - 抖音视频下载链接
    """
    try:
        processor  = DouyinProcessor("")
        video_info = processor.parse_share_url(share_link)
        return json.dumps({
            "status": "success",
            "video_id": video_info["video_id"],
            "title": video_info["title"],
            "download_url": video_info["url"],
            "cover_url": video_info["cover"],
            "description": f"视频标题: {video_info['title']}",
            "usage_tip": "可以直接使用此链接下载无水印视频"
        }, ensure_ascii=False, indent=2) 
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"获取下载链接失败: {str(e)}"
        }, ensure_ascii=False, indent=2)


    
@mcp.resource("douyin://video/{video_id}")
def get_video_info(video_id:str) ->str :
    """获取指定视频ID信息
    参数：
    - video_id: 视频ID
    返回值：
    - 视频信息
    """
    return f"视频ID: {video_id}\n视频标题: 抖音视频标题\n视频描述: 抖音视频描述\n发布时间: 2023-08-15 12:34:56\n作者: 抖音作者"

@mcp.prompt()
def douyin_text_extraction_guide() -> str:
    """ 抖音文本提取指南 """
    return """
    ## 使用步骤
    1. 复制抖音视频的分享链接
    2. 在Claude Desktop配置中设置环境变量 DOUYIN_API_KEY
    3. 使用相应的工具进行操作

    ## 工具说明
    - `extract_douyin_text`: 完整的文本提取流程（需要API密钥）
    - `get_douyin_download_link`: 获取无水印视频下载链接（无需API密钥）
    - `parse_douyin_video_info`: 仅解析视频基本信息
    - `douyin://video/{video_id}`: 获取指定视频的详细信息

    ## Claude Desktop 配置示例
    ```json
    {
    "mcpServers": {
        "douyin-mcp": {
        "command": "uvx",
        "args": ["douyin-mcp-server"],
        "env": {
            "DOUYIN_API_KEY": "your-SiliconFlow-api-key-here"
        }
        }
    }
    }
    """


def main():
    """启动MCP服务器"""
    mcp.run()
    #rocess = DouyinProcessor("sk-jrytgjrgrbzzkkfsrgtlutsaqpejadfcegjlideudfredtrv")
    #audio_path = process.extract_audio(Path("/Users/smartrui/Downloads/Untitled Video (3).mp4"))
    #text_content  = process.extract_text_from_audio(audio_path)
    #print(text_content)

if __name__ == '__main__':

    main()