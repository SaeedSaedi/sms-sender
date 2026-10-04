import os

from django.core.wsgi import get_wsgi_application
from dotenv import load_dotenv

# Local runs read `.env`; in Docker it's passed in as environment (env_file).
load_dotenv(".env")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sms_sender_web.settings")
application = get_wsgi_application()
