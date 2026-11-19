"""Public policy page; editors change only security-policy.txt."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from config.security_policy import SECURITY_POLICY_URL, security_policy_text
from web.templates import templates

router = APIRouter()


@router.get("/security-policy", response_class=HTMLResponse)
def security_policy():
    return HTMLResponse(
        templates.get_template("security-policy.html").render(
            policy_text=security_policy_text(), canonical_url=SECURITY_POLICY_URL,
        ),
        headers={"Cache-Control": "no-cache"},
    )
