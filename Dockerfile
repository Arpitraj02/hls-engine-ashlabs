# Use a lightweight Python image
FROM python:3.10-slim

# Install FFmpeg and system utilities
RUN apt-get update && apt-get install -y \
    ffmpeg \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Expose the port Render expects
EXPOSE 10000

# Start with optimized Uvicorn settings
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000", "--workers", "1"]
