from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..security.auth import Principal, authenticate
from ..state import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]
Auth = Annotated[Principal, Depends(authenticate)]
