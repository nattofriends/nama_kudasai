# nama_kudasai

🍻🍻🍻
Record livestreams and upload them to Dropbox
🍻🍻🍻

## Requirements
`ffmpeg`

## How to Configure
Copy `config.yaml.example` to `config.yaml` and fill in the values.
Get a `dropbox_api_access_token` from https://dropbox.tech/developers/generate-an-access-token-for-your-own-account.

## How to Run
Create a virtualenv and install `requirements.txt` into it.

Use `cron` to run `nama_kudasai.py` in the virtualenv every so often. It will spawn downloaders as appropriate. The downloaders will upload the archives to channel directories under the `nama_kudasai` directory in Dropbox.

## Caveats
- Live rewinding is currently broken in [Streamlink](https://streamlink.github.io/), the software used to download the stream. If a stream starts before a downloader is started for it, the past segments of the stream are not saved, even if they are viewable from YouTube itself at the time. See https://github.com/streamlink/streamlink/issues/2936 for more details.
- Downloader logs go to the `logs/` folder and there is no good way to surface if something abnormal has happened.

## Future
- Title filters?

## Warning
It is not known what kind of rate limiting or detection YouTube has on this kind of access.
