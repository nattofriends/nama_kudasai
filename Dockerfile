FROM python:3.8-slim-buster

# Install prereq packages
RUN apt-get update && apt-get install -y ffmpeg dumb-init

# Setting up the environment.mkv
ENV NAMA_TARGET=/opt/nama_kudasai
WORKDIR $NAMA_TARGET
RUN python -m venv $NAMA_TARGET/venv
ENV PATH="$NAMA_TARGET/venv/bin:$PATH"

# Install requirements
COPY requirements.txt .
RUN pip install -r requirements.txt

# gibe nama pls
COPY *.py $NAMA_TARGET/
RUN chmod +x $NAMA_TARGET/nama_runner.py
ENV PATH="$NAMA_TARGET:$PATH"

WORKDIR /data
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["nama_runner.py"]
