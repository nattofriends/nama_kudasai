from email.message import EmailMessage
from email.utils import make_msgid
from getpass import getuser
from smtplib import SMTP
from textwrap import dedent
import logging


MAIL_SERVER = 'localhost'

log = logging.getLogger(__name__)


def notify(channel, title, link, thumbnail):
    log.info(f'Sending notification for {title} on {channel}')
    msg = EmailMessage()
    # I suspect that too-long titles make the email module unhappy.
    msg['Subject'] = f'[nama_kudasai] {channel} finished a live stream'
    msg['From'] = 'nama_kudasai'
    msg['To'] = getuser()
    # Explicitly unthread messages
    msg['References'] = make_msgid()

    msg.set_content(dedent(f'''\
        {title}
        {channel}

        {link}
    '''))

    thumbnail_cid = make_msgid()
    msg.add_alternative(dedent(f'''\
        <html><body>
            <a style="text-decoration:none; color:#333" href="{link}">
                <div>
                    <img style="max-width:60%" src="cid:{thumbnail_cid[1:-1]}" />
                    <h1>{title}</h1>
                    <h2>{channel}</h2>
                </div>
            </a>

            <div itemscope itemtype="http://schema.org/EmailMessage">
                <meta itemprop="description" content="View video"/>
                <div itemprop="potentialAction" itemscope itemtype="http://schema.org/ViewAction">
                    <link itemprop="target url" href="{link}" />
                    <meta itemprop="name" content="View"/>
                  </div>
            </div>
        </body></html>
    '''), subtype='html')

    log.info(f'Thumbnail is {len(thumbnail)} bytes long')
    msg.get_payload()[1].add_related(thumbnail, 'image', 'png', cid=thumbnail_cid)

    with SMTP(MAIL_SERVER) as s:
        s.send_message(msg)
