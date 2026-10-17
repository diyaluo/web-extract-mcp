FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir mcp openpyxl && pip install --no-cache-dir --no-deps -e .
ENTRYPOINT ["python", "-m", "web_extract_mcp.server"]
