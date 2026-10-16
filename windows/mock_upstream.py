"""Local synthetic Responses server; no actual account or upstream access."""
import asyncio
import json
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, StreamingResponse

from .config import read_key


def create_mock(config):
    key = read_key(config.adapter.api_key_file, "cpa_")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def auth(request, call_next):
        if request.headers.get("authorization") != "Bearer " + key:
            return JSONResponse({"error": "invalid_key"}, status_code=401)
        return await call_next(request)

    @app.get("/v1/models")
    async def models():
        return {"data": [{"id": "mock-codex"}, {"id": "not-allowed"}]}

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        result = {"id": "resp_mock", "object": "response", "status": "completed", "model": "mock-codex",
                  "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "这是本机合成响应。"}]}],
                  "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 4, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 14}}
        if not body.get("stream"):
            return result

        async def events():
            for obj in ({"type": "response.output_text.delta", "delta": "这是本机"},
                        {"type": "response.output_text.delta", "delta": "合成响应。"},
                        {"type": "response.completed", "response": result}):
                data = ("event: " + obj["type"] + "\ndata: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
                for i in range(0, len(data), 7):
                    yield data[i:i + 7]
                await asyncio.sleep(.05)
        return StreamingResponse(events(), media_type="text/event-stream")
    return app
