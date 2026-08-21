FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

RUN addgroup --system mcp && adduser --system --ingroup mcp mcp

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY aem_mcp ./aem_mcp
COPY run_server.py run_http_server.py ./

USER mcp
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', os.getenv('MCP_PORT', '8000')) + '/health', timeout=3)"

CMD ["python", "run_http_server.py"]
