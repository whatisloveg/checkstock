from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from app import web

app = FastAPI(title="CheckStock")
app.include_router(web.router)
app.add_exception_handler(RequestValidationError, web.invalid_form)
app.add_exception_handler(HTTPException, web.page_not_found)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
