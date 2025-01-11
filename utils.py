import os
import requests
import json
import pytz
import time
from datetime import datetime
from subprocess import check_output
from pytz import timezone 
from urllib.request import urlopen, Request
import shlex
import ffmpeg
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

iptv_link = "https://gist.githubusercontent.com/kunani1/a048909a292d308d63dabc72acb58200/raw/34f9288582d480d9eea490d0937b57416c448e0d/links.json"

def fetch_data(url):
    data = requests.get(url)
    data = data.text
    return json.loads(data)

def getChannels(app, message):
    data = fetch_data(iptv_link)
    channelsList = ""
    for i in data:
        channelsList += f"{i}\n"
    message.reply_text(text=f"Available Channels:\n\n{channelsList}")
