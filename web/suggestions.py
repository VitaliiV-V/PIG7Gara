"""Text submissions without Telegram sign-in; IP and a technical cookie support bans."""

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from bot.suggestion_store import SubmissionError, SuggestionStore
from web.templates import templates

router = APIRouter()
store = SuggestionStore()
COOKIE = "gara_submitter"
TOKEN = re.compile(r"^[a-f0-9]{64}$")
NONCE = re.compile(r"^[a-f0-9]{24}$")
WEB_DIR = Path(__file__).resolve().parent
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def csrf(cookie):
    return hmac.new(cookie.encode(), b"gara-suggestion-form-v1", hashlib.sha256).hexdigest()


def page(cookie, *, error=None, text="", receipt=None, status=200, request_id=None):
    return HTMLResponse(templates.get_template("suggest.html").render(
        csrf=csrf(cookie), request_id=request_id or secrets.token_hex(12),
        error=error, text=text, receipt=receipt,
    ), status_code=status, headers=HEADERS)


@router.get("/suggest", response_class=HTMLResponse)
def form(request: Request):
    cookie = request.cookies.get(COOKIE, "")
    fresh = not TOKEN.fullmatch(cookie)
    if fresh:
        cookie = secrets.token_hex(32)
    response = page(cookie)
    if fresh:
        response.set_cookie(COOKIE, cookie, max_age=180 * 86400, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/suggest")
    return response


@router.get("/suggest.css")
def styles():
    return FileResponse(WEB_DIR / "suggest.css")


@router.get("/suggest.js")
def script():
    return FileResponse(WEB_DIR / "suggest.js")


@router.post("/suggest", response_class=HTMLResponse)
async def submit(request: Request):
    cookie = request.cookies.get(COOKIE, "")
    data = {}
    json_response = "application/json" in request.headers.get("accept", "")
    try:
        if not TOKEN.fullmatch(cookie):
            raise SubmissionError("Разрешите техническую cookie и заново откройте форму.", 403)
        origin = request.headers.get("origin")
        expected = urlsplit(str(request.base_url))
        if origin and origin != f"{expected.scheme}://{expected.netloc}":
            raise SubmissionError("Отправляйте заявку со страницы формы.", 403)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise SubmissionError("Неподдерживаемый формат заявки.", 415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 40000:
                raise SubmissionError("Заявка слишком большая.", 413)
        try:
            fields = parse_qs(body.decode("utf-8"), max_num_fields=8, errors="strict")
        except (UnicodeError, ValueError):
            raise SubmissionError("Не удалось прочитать заявку.") from None
        if any(len(values) != 1 for values in fields.values()):
            raise SubmissionError("Повторяющиеся поля заявки.")
        data = {key: values[0] for key, values in fields.items()}
        if not hmac.compare_digest(data.get("csrf", "").encode(), csrf(cookie).encode()):
            raise SubmissionError("Форма устарела. Обновите страницу.", 403)
        if not NONCE.fullmatch(data.get("request_id", "")):
            raise SubmissionError("Обновите страницу формы.")
        text = data.get("text", "").strip()
        if not text or len(text.encode("utf-16-le")) // 2 > 3000:
            raise SubmissionError("Напишите от 1 до 3000 символов.")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            raise SubmissionError("Уберите служебные символы из текста.")
        if data.get("consent") != "yes":
            raise SubmissionError("Подтвердите условия отправки.")
        if not request.client:
            raise SubmissionError("Не удалось определить IP-адрес. Попробуйте позже.", 503)
        browser_hash = hashlib.sha256(cookie.encode()).hexdigest()
        request_key = hashlib.sha256(f"{browser_hash}:{data['request_id']}".encode()).hexdigest()
        receipt = await run_in_threadpool(store.submit, text, request.client.host, browser_hash, request_key)
        if json_response:
            return JSONResponse({"receipt": receipt}, headers=HEADERS, status_code=201)
        return page(cookie, receipt=receipt, status=201)
    except SubmissionError as exc:
        if json_response:
            return JSONResponse({"error": str(exc)}, status_code=exc.status, headers=HEADERS)
        return page(cookie, error=str(exc), text=data.get("text", "")[:3000],
                    request_id=data.get("request_id") if NONCE.fullmatch(data.get("request_id", "")) else None,
                    status=exc.status)
