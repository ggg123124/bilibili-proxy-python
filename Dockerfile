# 使用阿里云的 Python 3.11 镜像作为基础镜像
FROM registry.cn-hangzhou.aliyuncs.com/library/python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
# 防止 Python 生成 .pyc 文件
ENV PYTHONDONTWRITEBYTECODE=1
# 禁用 Python 输出缓冲
ENV PYTHONUNBUFFERED=1



# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖（使用阿里云 pip 镜像源）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 复制应用代码
COPY main.py .

# 暴露端口
EXPOSE 8000

# 启动应用
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
