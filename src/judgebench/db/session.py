from functools import cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from judgebench.config import settings


@cache
def sessions() -> sessionmaker[Session]:
    return sessionmaker(create_engine(settings().database_url, pool_pre_ping=True), expire_on_commit=False)
