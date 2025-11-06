# Railway Deployment Fixes - Complete Guide

This document outlines all the fixes applied to resolve the 502 errors on Railway deployment.

## 🔴 Issues Fixed

### 1. Chrome Headless Mode Not Enabled
**Problem**: Chrome was trying to open a GUI window in Railway's containerized environment  
**Solution**: Enabled headless mode with `--headless=new`

### 2. Missing System Dependencies
**Problem**: Chrome requires specific libraries to run in containers  
**Solution**: Added all required libraries (libgbm1, libnss3, libatk-bridge2.0-0, etc.)

### 3. Deprecated apt-key Command
**Problem**: `apt-key` is deprecated and causing build failures  
**Solution**: Updated to modern GPG key management using `/usr/share/keyrings/`

### 4. No Health Check Endpoint
**Problem**: Railway couldn't verify the service was healthy  
**Solution**: Added `/health` endpoint and configured `railway.toml`

### 5. GET /otp Returning 502
**Problem**: `/otp` was POST-only, causing confusion  
**Solution**: Added GET `/otp` endpoint with usage information

---

## 📝 Changes Made

### 1. `main.py`
- ✅ Enabled Chrome headless mode
- ✅ Added CORS middleware
- ✅ Added `/health` health check endpoint
- ✅ Added GET `/otp` endpoint for usage information
- ✅ Enhanced startup logging
- ✅ Added Chrome initialization logging

### 2. `Dockerfile`
- ✅ Fixed GPG key installation (modern method)
- ✅ Added all required Chrome dependencies
- ✅ Improved ChromeDriver installation
- ✅ Added verification steps for Chrome and ChromeDriver
- ✅ Used Python for JSON parsing (more reliable)

### 3. `railway.toml`
- ✅ Added health check configuration
- ✅ Set health check path to `/health`
- ✅ Configured 300s timeout for health checks

### 4. `.dockerignore` (New)
- ✅ Excluded unnecessary files from Docker build
- ✅ Faster build times

### 5. `test_endpoints.sh` (New)
- ✅ Script to test all API endpoints
- ✅ Works locally or on Railway

---

## 🚀 Deployment Steps

### Step 1: Commit All Changes
```bash
git add .
git commit -m "fix: Enable headless Chrome and add health checks for Railway"
git push origin main
```

### Step 2: Monitor Railway Logs
1. Go to your Railway dashboard
2. Click on your project
3. Go to "Deployments" tab
4. Click on the latest deployment
5. Watch the build logs for:
   - ✅ "Google Chrome 131.x.x.x" (version number)
   - ✅ "ChromeDriver x.x.x.x" (version number)
   - ✅ "🚀 Progressive Policy Bot - Starting Up"
   - ✅ "Application startup complete"

### Step 3: Test the Endpoints
Once deployed, test using the provided script:

```bash
# Test Railway deployment
./test_endpoints.sh https://policybot-production.up.railway.app
```

Or manually test:
```bash
# Health check
curl https://policybot-production.up.railway.app/health

# Root endpoint
curl https://policybot-production.up.railway.app/

# OTP info
curl https://policybot-production.up.railway.app/otp

# OTP submission
curl -X POST https://policybot-production.up.railway.app/otp \
  -H "Content-Type: application/json" \
  -d '{"otp": "123456"}'
```

---

## 🧪 Expected Results

### Successful Deployment Should Show:
1. ✅ Build completes without errors
2. ✅ Chrome version displayed in logs
3. ✅ ChromeDriver version displayed in logs
4. ✅ Server starts on correct port
5. ✅ "Application startup complete" message
6. ✅ All endpoints return 200 status codes

### What You Should See:
```
============================================================
🚀 Progressive Policy Bot - Starting Up
============================================================
📡 Port: 8082
🌐 Host: 0.0.0.0
🐍 Python: 3.11.x
📁 Working Directory: /app
============================================================
INFO:     Started server process [1]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8082 (Press CTRL+C to quit)
```

---

## 🐛 Troubleshooting

### If You Still Get 502 Errors:

1. **Check Railway Logs for Errors**
   - Look for "ERROR" or "FAILED" messages
   - Check if Chrome/ChromeDriver installed successfully

2. **Verify Port Configuration**
   - Railway sets `PORT` environment variable automatically (typically 8080)
   - Our app reads it: `port = int(os.environ.get('PORT', 8080))`

3. **Check Memory Limits**
   - Chrome needs at least 512MB RAM
   - Upgrade Railway plan if needed

4. **Test Locally First**
   ```bash
   # Build Docker image locally
   docker build -t policybot .
   
   # Run container (use PORT env var or default to 8080)
   docker run -p 8080:8080 -e PORT=8080 policybot
   
   # Test endpoints
   ./test_endpoints.sh http://localhost:8080
   ```

5. **Railway-Specific Issues**
   - Check Railway status page: https://railway.app/status
   - Verify your Railway project has enough resources
   - Check if the correct branch is deployed

---

## 📊 Endpoints Summary

| Method | Endpoint | Description | Returns |
|--------|----------|-------------|---------|
| GET | `/` | Root/info endpoint | API information |
| GET | `/health` | Health check | Service status |
| GET | `/otp` | OTP usage info | How to submit OTP |
| POST | `/otp` | Submit OTP code | Success confirmation |
| GET | `/otp/status` | Check OTP status | OTP availability |
| POST | `/retrieve-policy` | Get policy PDF | PDF file |

---

## ✅ Verification Checklist

- [ ] Code pushed to repository
- [ ] Railway build completed successfully
- [ ] Chrome version shown in build logs
- [ ] ChromeDriver version shown in build logs
- [ ] Server startup logs visible
- [ ] GET `/` returns 200
- [ ] GET `/health` returns 200
- [ ] GET `/otp` returns 200
- [ ] POST `/otp` returns 200
- [ ] GET `/otp/status` returns 200

---

## 📞 Need Help?

If you're still experiencing issues after following this guide:

1. Share the Railway deployment logs (look for errors)
2. Run the test script and share the output
3. Check if Chrome/ChromeDriver versions are compatible

---

**Last Updated**: October 28, 2025

