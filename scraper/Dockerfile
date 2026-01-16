FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY scraper.py .

# Expose port
EXPOSE 5001

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=scraper.py

# Run the application
CMD ["python", "scraper.py"]
