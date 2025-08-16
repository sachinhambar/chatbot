# Chatbot Deployment Guide

This project contains a FastAPI backend and a static web client. The instructions below show how to deploy them on an Ubuntu server using **nginx** as a reverse proxy. The API will be available at `api.domain.com` and the web client at `web.domain.com`.

## 1. Server Preparation
1. Update packages and install Python dependencies for the API:
    ```bash
    sudo apt update
    sudo apt install python3 python3-venv -y
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install uvicorn fastapi
    pip install -e ./server
    ```
2. (Optional) Build or copy the static client files to `/var/www/web`:
    ```bash
    sudo mkdir -p /var/www/web
    sudo cp -r client/* /var/www/web/
    ```

## 2. Running the API
Start the FastAPI server behind nginx on port 8000:
```bash
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```
Run this command in a process manager such as `systemd` or `tmux` for production use.

## 3. Install nginx
```bash
sudo apt update
sudo apt install nginx -y
```
Enable and start nginx:
```bash
sudo systemctl enable nginx
sudo systemctl start nginx
```

## 4. Configure nginx
Create configuration files for the API and web client.

### API proxy (api.domain.com)
```nginx
server {
    listen 80;
    server_name api.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### Web client (web.domain.com)
```nginx
server {
    listen 80;
    server_name web.domain.com;

    root /var/www/web;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

Save each block in `/etc/nginx/sites-available/` (for example `api.conf` and `web.conf`) and enable them:
```bash
sudo ln -s /etc/nginx/sites-available/api.conf /etc/nginx/sites-enabled/
sudo ln -s /etc/nginx/sites-available/web.conf /etc/nginx/sites-enabled/

sudo nginx -t
sudo systemctl reload nginx
```

## 5. GoDaddy DNS setup
If you purchased a domain from GoDaddy:
1. Log in to GoDaddy and open the DNS management page for your domain.
2. Create **A** records pointing to your server's public IP address:
    - Name: `api`  →  points to your server IP
    - Name: `web`  →  points to your server IP
3. Allow up to a few minutes for DNS propagation.

After DNS propagates, navigating to `https://api.domain.com` should reach the API and `https://web.domain.com` should load the web client (consider using Let's Encrypt for HTTPS).

## 6. Firewall (optional)
Make sure ports 80 (HTTP) and 443 (HTTPS) are open:
```bash
sudo ufw allow 'Nginx Full'
```

You're now ready to serve both the API and the web client using nginx on Ubuntu.
