#!/usr/bin/env python3
"""Shared MySQL connection helper for FleetCast.

Configuration comes from the environment so the same code runs against a local
MySQL and against PythonAnywhere's, with no credentials in the repo:

    FLEETCAST_DB_HOST      default 127.0.0.1
    FLEETCAST_DB_PORT      default 3306
    FLEETCAST_DB_USER      default root
    FLEETCAST_DB_PASSWORD  default "" (empty, as a local dev install usually is)
    FLEETCAST_DB_NAME      default fleetcast
"""

from __future__ import annotations

import os

import pymysql
from pymysql.cursors import DictCursor


def config() -> dict:
    return {
        "host": os.environ.get("FLEETCAST_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("FLEETCAST_DB_PORT", "3306")),
        "user": os.environ.get("FLEETCAST_DB_USER", "root"),
        "password": os.environ.get("FLEETCAST_DB_PASSWORD", ""),
        "database": os.environ.get("FLEETCAST_DB_NAME", "fleetcast"),
    }


def connect(dict_rows: bool = False, autocommit: bool = False):
    settings = config()
    return pymysql.connect(
        **settings,
        charset="utf8mb4",
        autocommit=autocommit,
        cursorclass=DictCursor if dict_rows else pymysql.cursors.Cursor,
    )
