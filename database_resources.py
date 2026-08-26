import atexit
import time

import streamlit as st

import db as db_module
from performance import record_database_call


@st.cache_resource(show_spinner=False)
def get_postgres_pool(database_url):
    started = time.perf_counter()
    pool = db_module.Database.create_pool(database_url)
    record_database_call(
        "pool_create",
        (time.perf_counter() - started) * 1000,
        "get_postgres_pool",
    )
    try:
        started = time.perf_counter()
        db_module.initialize_postgres_schema(pool)
        record_database_call(
            "pool_schema_check",
            (time.perf_counter() - started) * 1000,
            "get_postgres_pool",
        )
    except db_module.DatabaseError:
        pool.close()
        raise
    atexit.register(pool.close)
    return pool


def get_database(database_url):
    return db_module.Database(pool=get_postgres_pool(database_url))
