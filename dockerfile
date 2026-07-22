RUN apt-get update && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages -r requirements.txt
