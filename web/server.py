from config.logging_config import configure_logging

configure_logging()

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

from web.policy import router as policy_router
from web.suggestions import router as suggestions_router
from web.templates import templates

app = FastAPI(title="Pig 7 Gara")
app.include_router(policy_router)
app.include_router(suggestions_router)


@app.get("/")
def index():
    return HTMLResponse(templates.get_template("index.html").render())


@app.get("/common.css")
def get_styles():
    return FileResponse("web/common.css")


@app.get("/site.js")
def get_styles():
    return FileResponse("web/site.js")


@app.exception_handler(404)
async def not_found(request, exc):

    return HTMLResponse(
        templates.get_template("404.html").render(),
        status_code=404,
    )


def run_api():
    uvicorn.run(app, host="0.0.0.0", port=2322, log_level="info", access_log=False)


if __name__ == "__main__":
    run_api()
