# OTP Timeout Issue - Fix Guide

## Changes Made

### 1. **Optimized OTP Endpoint**
- ✅ Moved `import re` to top of file (no longer imported on every request)
- ✅ Added immediate request logging
- ✅ Added response timing logging

### 2. **Added Request Timing Middleware**
- Tracks how long each request takes
- Shows in Railway logs: `Request completed: POST /otp - 0.123s`

### 3. **Added Startup Event**
- Confirms when FastAPI is ready to accept requests
- Shows: "🚀 FastAPI Application Startup Complete"

---

## Deploy the Changes

```bash
cd /Users/user/Desktop/policybot
git add main.py
git commit -m "fix: Optimize OTP endpoint and add request timing"
git push
```

---

## What to Look for in Railway Logs

### On Deployment Success:
```
============================================================
🚀 Progressive Policy Bot - Starting Up
============================================================
📡 Port: 8082
🌐 Host: 0.0.0.0
...
============================================================
============================================================
🚀 FastAPI Application Startup Complete
============================================================
📡 Server is ready to accept requests
🔗 Health check: /health
🔗 OTP endpoint: POST /otp
============================================================
INFO:     Application startup complete.
```

### When OTP Request Arrives:
```
🔵 Incoming request: POST /otp
📨 OTP endpoint hit at 2025-10-28 14:30:45
📦 Request data: {'otp': '711600'}
✅ OTP received via API: 711600
⏱️  OTP endpoint processing complete
✅ Request completed: POST /otp - 0.025s
```

**Important**: The request should complete in **< 1 second**, not 15+ seconds!

---

## Testing After Deployment

### Test 1: Health Check (Should be instant)
```bash
time curl https://policybot-production.up.railway.app/health
```
Expected: **< 1 second**

### Test 2: OTP Endpoint (Should be instant)
```bash
time curl -X POST https://policybot-production.up.railway.app/otp \
  -H "Content-Type: application/json" \
  -d '{"otp": "123456"}'
```
Expected: **< 1 second**

### Test 3: From Your OTP Sending Code
Update your timeout from 15s to 30s while we debug:
```python
import requests

response = requests.post(
    'https://policybot-production.up.railway.app/otp',
    json={'otp': '711600'},
    timeout=30  # Increased from 15
)
```

---

## Common Issues & Solutions

### Issue 1: App Takes 15+ Seconds to Respond

**Possible Causes:**
1. **Cold Start**: Railway is starting the container (first request after idle)
2. **Resource Limits**: Not enough memory/CPU allocated
3. **Chrome Initialization**: Setup happening on first request

**Solutions:**
- Wait for "Application startup complete" in logs before sending OTP
- Upgrade Railway plan for more resources
- Keep the app warm with periodic health checks

### Issue 2: Connection Timeout
**Symptom**: `ReadTimeoutError` or `Connection refused`

**Solutions:**
- Check Railway deployment status (is it actually running?)
- Check Railway logs for errors
- Verify the domain is correct

### Issue 3: Still Getting 502 Errors
**Symptom**: `502 Bad Gateway`

**Solutions:**
- The app crashed - check Railway logs for Python errors
- Chrome failed to start - verify Dockerfile built correctly
- Port mismatch - verify Railway sets PORT env var

---

## Debugging Checklist

- [ ] Code pushed to Railway
- [ ] Deployment succeeded (green checkmark in Railway)
- [ ] Logs show "Application startup complete"
- [ ] Health endpoint responds < 1s
- [ ] OTP endpoint responds < 1s
- [ ] Railway logs show request timing
- [ ] No Python errors in logs

---

## Keep App Warm (Prevent Cold Starts)

If Railway is hibernating your app between requests, keep it warm:

```bash
# Run this every 5 minutes from a cron job or monitoring service
curl https://policybot-production.up.railway.app/health
```

Or use a service like:
- UptimeRobot (free tier)
- Cron-job.org
- Better Stack

---

## Railway Resource Settings

Recommended minimum:
- **Memory**: 1 GB (Chrome needs memory)
- **vCPU**: 1 vCPU
- **Restart Policy**: On Failure
- **Health Check**: Enabled (`/health`)

Check your Railway project settings to increase resources if needed.

---

## Still Having Issues?

1. **Share Railway Logs**: Copy the full deployment logs and share
2. **Test Locally**: Build and run Docker container locally to verify it works
3. **Check Railway Status**: https://railway.app/status
4. **Timing Test**: Run both curl commands above and share the times

---

**Last Updated**: October 28, 2025

