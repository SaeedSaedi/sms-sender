#!/usr/bin/env python
"""Django's command-line utility for the dashboard (pip install -e ".[web]")."""
import os
import sys

from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv(".env")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sms_sender_web.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
