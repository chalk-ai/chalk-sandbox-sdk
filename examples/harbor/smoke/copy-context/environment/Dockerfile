FROM ubuntu:24.04
COPY assets/ /opt/assets/
RUN test -s /opt/assets/words.txt && wc -w < /opt/assets/words.txt > /opt/assets/word_count
WORKDIR /app
