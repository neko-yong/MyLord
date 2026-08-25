import atexit

import streamlit as st

import db as db_module


@st.cache_resource(show_spinner=False)
def get_postgres_pool(database_url):
    pool = db_module.Database.create_pool(database_url)
    try:
        db_module.initialize_postgres_schema(pool)
    except db_module.DatabaseError:
        pool.close()
        raise
    atexit.register(pool.close)
    return pool


def get_database(database_url):
    return db_module.Database(pool=get_postgres_pool(database_url))
