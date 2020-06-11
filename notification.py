from email.message import EmailMessage
from email.utils import make_msgid
from getpass import getuser
from smtplib import SMTP
from textwrap import dedent


MAIL_SERVER = 'localhost'


def notify(channel, title, link, thumbnail):
    msg = EmailMessage()
    msg['Subject'] = f'[nama_kudasai] {channel} uploaded {title}'
    msg['From'] = 'nama_kudasai'
    msg['To'] = getuser()

    msg.set_content(dedent(f'''\
        {channel} uploaded {title}

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
        </body></html>
    '''), subtype='html')

    msg.get_payload()[1].add_related(thumbnail, 'image', 'png', cid=thumbnail_cid)

    with SMTP(MAIL_SERVER) as s:
        s.send_message(msg)
