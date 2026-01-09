"""
Progressive Insurance Driver Add/Update Bot - FastAPI Application

This application automates the process of logging into Progressive's ForAgentsOnly portal,
adding or updating drivers on policies, and extracting policy information using Selenium WebDriver.
Also supports vehicle add/replace operations.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.keys import Keys
import os
import time
import re
import asyncio
from pathlib import Path
import json
import threading
import queue
from typing import Dict, Optional

# Thread-safe OTP queue system for multi-threaded browser instances
# OTPs are distributed in FIFO order: first OTP goes to first browser, second OTP to second browser, etc.
otp_queue = queue.Queue()
otp_queue_lock = threading.Lock()

# Thread registration system to ensure proper FIFO ordering
# Threads register when they start waiting, and OTPs are distributed based on registration order
otp_waiting_threads = []  # List of thread IDs waiting for OTP in order
otp_waiting_lock = threading.Lock()

# Thread counter for assigning unique thread IDs to each browser instance
thread_counter = 0
thread_counter_lock = threading.Lock()

# Thread ID to browser mapping for logging purposes
browser_threads: Dict[int, dict] = {}
browser_threads_lock = threading.Lock()

# Port counter for unique remote debugging ports (each browser needs unique port)
debug_port_counter = 9222
debug_port_lock = threading.Lock()

# Legacy global OTP storage for backward compatibility (kept for safety)
otp_storage = {"otp": None, "timestamp": None}

# Initialize FastAPI app
app = FastAPI(
    title="Progressive Driver Add/Update Bot",
    description="Automates driver add/update and vehicle operations on Progressive ForAgentsOnly portal",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Add request timing middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Log all incoming requests with timing information and status codes.
    Skips logging for health checks and favicon requests to keep logs clean.
    """
    start_time = time.time()
    
    # Skip logging for health checks and favicon (Railway automated requests)
    skip_paths = ["/health", "/favicon.ico"]
    should_log = request.url.path not in skip_paths
    
    # Log detailed request information (only for non-health-check requests)
    if should_log:
        print("=" * 60)
        print(f"🔵 Incoming request: {request.method} {request.url.path}")
        print(f"   Client: {request.client.host if request.client else 'Unknown'}")
        print(f"   Full URL: {request.url}")
        
        # Log query parameters if any
        if request.url.query:
            print(f"   Query: {request.url.query}")
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    
    # Log completion (only for non-health-check requests)
    if should_log:
        # Determine emoji based on status code
        if response.status_code < 300:
            status_emoji = "✅"
        elif response.status_code < 400:
            status_emoji = "🔄"
        elif response.status_code < 500:
            status_emoji = "⚠️"
        else:
            status_emoji = "❌"
        
        print(f"{status_emoji} Request completed: {request.method} {request.url.path} - Status: {response.status_code} - {process_time:.3f}s")
        print("=" * 60)
    
    return response


@app.on_event("startup")
async def startup_event():
    """
    Startup event handler - runs when the application starts.
    """
    port = os.environ.get('PORT', 'Not set (using default)')
    environment = os.environ.get('RAILWAY_ENVIRONMENT', 'local')
    
    print("=" * 60)
    print("🚀 FastAPI Application Ready")
    print("=" * 60)
    print(f"🔌 Port: {port}")
    print(f"🌍 Environment: {environment}")
    print(f"📡 API Endpoints:")
    print(f"   • POST /start - Start driver add/update or vehicle automation")
    print(f"   • POST /otp - Submit OTP code")
    print(f"   • GET /otp/status - Check if OTP is needed")
    print("=" * 60)


class PolicyRequest(BaseModel):
    """
    Request payload model for policy retrieval.
    
    Required fields for 'add driver' or 'update driver' action:
        - username, password, policy_no, action_type, date_to_add_driver, agent_name, driver_first_name, driver_last_name, driver_dob, driver_gender, driver_marital_status
    
    Required fields for vehicle actions ('add vehical' or 'replace vehical'):
        - username, password, policy_no, action_type, date_to_rep_vehical, agent_name
        - Plus all vehicle-specific fields listed below
    
    Attributes:
        username: Progressive ForAgentsOnly username
        password: Progressive ForAgentsOnly password
        policy_no: Policy number to retrieve
        action_type: Action type: 'add driver', 'update driver', 'add vehical', or 'replace vehical'
        date_to_add_driver: Date to add driver (format: mm/dd/yyyy, required for 'add driver' or 'update driver', not used for vehicle actions)
        date_to_rep_vehical: Date to replace vehicle (format: mm/dd/yyyy, required for vehicle actions, not used for driver actions)
        agent_name: Agent contact name
        driver_first_name: Driver first name (required for 'add driver' or 'update driver' actions, not used for vehicle actions)
        driver_last_name: Driver last name (required for 'add driver' or 'update driver' actions, not used for vehicle actions)
        driver_dob: Driver date of birth (format: mm/dd/yyyy, required for 'add driver' or 'update driver' actions, not used for vehicle actions)
        driver_gender: Driver gender ('male' or 'female', required for 'add driver' or 'update driver' actions, not used for vehicle actions)
        driver_marital_status: Driver marital status ('married' or 'single', required for 'add driver' or 'update driver' actions, not used for vehicle actions)
        
    Vehicle-specific fields (optional, only used for vehicle actions, kept for future reference):
        vehicle_name_to_replace: Vehicle name to replace (required only for 'replace vehical', can be partial, e.g., 'CHEVROLET SUBURBAN')
        vehical_year: Year of the new vehicle (e.g., '2024', required for vehicle actions)
        vehical_is_suv_van_pickup: Whether vehicle is conversion van/pickup/SUV ('yes' or 'no', required for vehicle actions)
        vehical_is_kitcar_buggy_classic: Whether vehicle is kit car/buggy/classic ('yes' or 'no', required for vehicle actions)
        make: Vehicle make (e.g., 'TOYOTA', 'HONDA', 'CHEVROLET', required for vehicle actions)
        model: Vehicle model (e.g., 'CAMRY', 'ACCORD', 'X5', required for vehicle actions)
        vehicle_use: Vehicle use type: 'Commute', 'Pleasure/Personal', 'Business', or 'Farm' (required for vehicle actions)
        vehicle_use_ridesharing: Whether vehicle is used for ridesharing ('yes' or 'no', required for vehicle actions)
        one_way_commute_miles: One-way commute miles (max 3 digits, e.g., '15', required for vehicle actions)
        vehicle_ownership: Vehicle ownership type: 'Lease', 'Own and make payments', or 'Own and do not make payments' (required for vehicle actions)
        comprehensive_deductible: Comprehensive deductible: 'No Coverage', '$100 deductible', '$250 deductible', '$500 deductible', '$750 deductible', '$1,000 deductible', '$1,500 deductible', '$2,000 deductible', or with '$0 Glass deductible' option (required for vehicle actions)
        medical_payment_coverage: Medical payment coverage: 'No Coverage', '$500 each person', '$1,000 each person', '$2,000 each person', '$5,000 each person', or '$10,000 each person' (required for vehicle actions)
        collision_deductible: Collision deductible: 'No Coverage', '$100 deductible', '$250 deductible', '$500 deductible', '$750 deductible', '$1,000 deductible', '$1,500 deductible', or '$2,000 deductible' (required for vehicle actions)
        bodily_injury_property_damage: Bodily injury and property damage liability: Split limits like '$100,000 each person/$300,000 each accident/$100,000 each accident' or combined single limits like '$300,000 combined single limit' (required for vehicle actions)
    """
    username: str = Field(..., description="ForAgentsOnly username")
    password: str = Field(..., description="ForAgentsOnly password")
    policy_no: str = Field(..., description="Policy number to retrieve")
    action_type: str = Field(..., description="Action type: 'add driver', 'update driver', 'add vehical', or 'replace vehical'")
    date_to_add_driver: str = Field(default="", description="Date to add driver (format: mm/dd/yyyy, e.g., 10/31/2025, required for 'add driver' or 'update driver')")
    date_to_rep_vehical: str = Field(default="", description="Date to replace vehicle (format: mm/dd/yyyy, e.g., 10/31/2025, required for vehicle actions)")
    agent_name: str = Field(..., description="Agent contact name")
    driver_first_name: str = Field(default="", description="Driver first name (required for 'add driver' or 'update driver' actions)")
    driver_last_name: str = Field(default="", description="Driver last name (required for 'add driver' or 'update driver' actions)")
    driver_dob: str = Field(default="", description="Driver date of birth (format: mm/dd/yyyy, e.g., 01/15/1990, required for 'add driver' or 'update driver' actions)")
    driver_gender: str = Field(default="", description="Driver gender: 'male' or 'female' (required for 'add driver' or 'update driver' actions)")
    driver_marital_status: str = Field(default="", description="Driver marital status: 'married' or 'single' (required for 'add driver' or 'update driver' actions)")
    vehicle_name_to_replace: str = Field(default="", description="Vehicle name to replace (required only for 'replace vehical', can be partial, e.g., 'CHEVROLET SUBURBAN')")
    vehical_year: str = Field(default="", description="Year of the new vehicle (e.g., '2024', required for vehicle actions)")
    vehical_is_suv_van_pickup: str = Field(default="", description="Whether vehicle is conversion van/pickup/SUV ('yes' or 'no', required for vehicle actions)")
    vehical_is_kitcar_buggy_classic: str = Field(default="", description="Whether vehicle is kit car/buggy/classic ('yes' or 'no', required for vehicle actions)")
    make: str = Field(default="", description="Vehicle make (e.g., 'TOYOTA', 'HONDA', 'CHEVROLET', required for vehicle actions)")
    model: str = Field(default="", description="Vehicle model (e.g., 'CAMRY', 'ACCORD', 'X5', required for vehicle actions)")
    vehicle_use: str = Field(default="", description="Vehicle use type: 'Commute', 'Pleasure/Personal', 'Business', or 'Farm' (required for vehicle actions)")
    vehicle_use_ridesharing: str = Field(default="", description="Whether vehicle is used for ridesharing ('yes' or 'no', required for vehicle actions)")
    one_way_commute_miles: str = Field(default="", description="One-way commute miles (max 3 digits, e.g., '15', required for vehicle actions)")
    vehicle_ownership: str = Field(default="", description="Vehicle ownership type: 'Lease', 'Own and make payments', or 'Own and do not make payments' (required for vehicle actions)")
    comprehensive_deductible: str = Field(default="", description="Comprehensive deductible: 'No Coverage', '$100 deductible', '$250 deductible', '$500 deductible', '$750 deductible', '$1,000 deductible', '$1,500 deductible', '$2,000 deductible', or with '$0 Glass deductible' option (required for vehicle actions)")
    medical_payment_coverage: str = Field(default="", description="Medical payment coverage: 'No Coverage', '$500 each person', '$1,000 each person', '$2,000 each person', '$5,000 each person', or '$10,000 each person' (required for vehicle actions)")
    collision_deductible: str = Field(default="", description="Collision deductible: 'No Coverage', '$100 deductible', '$250 deductible', '$500 deductible', '$750 deductible', '$1,000 deductible', '$1,500 deductible', or '$2,000 deductible' (required for vehicle actions)")
    bodily_injury_property_damage: str = Field(default="", description="Bodily injury and property damage liability: Split limits like '$100,000 each person/$300,000 each accident/$100,000 each accident' or combined single limits like '$300,000 combined single limit' (required for vehicle actions)")


def get_next_debug_port() -> int:
    """
    Get the next available remote debugging port for a browser instance.
    Each browser needs a unique port to avoid conflicts.
    
    Returns:
        int: Unique remote debugging port number
    """
    global debug_port_counter
    
    with debug_port_lock:
        debug_port_counter += 1
        # Use ports starting from 9223 (9222 is often used by default Chrome instances)
        # Incrementing for each browser to ensure uniqueness
        # Limit to reasonable range (9223-9999)
        if debug_port_counter < 9223:
            debug_port_counter = 9223  # Start from 9223
        if debug_port_counter > 9999:
            debug_port_counter = 9223  # Reset if we exceed range (ports should be freed by then)
        
        assigned_port = debug_port_counter
        
        # No delay - browsers should open instantly
        # Chrome handles port assignment internally, no need for artificial delay
        
        return assigned_port


def setup_chrome_driver(debug_port: Optional[int] = None):
    """
    Configure and initialize Chrome WebDriver with appropriate options.
    
    Args:
        debug_port: Optional remote debugging port. If not provided, a unique port will be assigned.
    
    Returns:
        webdriver.Chrome: Configured Chrome WebDriver instance
        
    Note:
        - Runs in headless mode (browser window will not be visible)
        - Uses persistent Chrome profiles to maintain login sessions (no repeated OTPs)
        - Each concurrent browser gets its own profile directory
        - Disables GPU and sandbox for compatibility
        - Sets download directory for PDF files
        - Each browser instance gets a unique remote debugging port
    """
    chrome_options = Options()
    
    # Get unique debug port for this browser instance
    if debug_port is None:
        debug_port = get_next_debug_port()
    
    # Headless mode ENABLED - browser window will not be visible
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument(f"--remote-debugging-port={debug_port}")
    
    # Additional options to speed up browser startup
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-logging")
    chrome_options.add_argument("--log-level=3")  # Suppress most logs
    chrome_options.add_argument("--silent")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    
    # Add logging for debugging
    print(f"🔧 Initializing Chrome WebDriver in headless mode (debug port: {debug_port})...")
    
    # Set up persistent Chrome profile to avoid repeated OTP authentication
    # Each thread gets its own profile directory to avoid conflicts
    chrome_profiles_dir = os.path.join(os.getcwd(), "chrome_profiles")
    os.makedirs(chrome_profiles_dir, exist_ok=True)
    
    # Create a unique profile directory for this debug port
    # This ensures each concurrent browser has its own profile
    profile_dir = os.path.join(chrome_profiles_dir, f"profile_{debug_port}")
    os.makedirs(profile_dir, exist_ok=True)
    
    # Configure Chrome to use the persistent profile
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    # Use a specific profile name to avoid conflicts
    chrome_options.add_argument("--profile-directory=Default")
    
    print(f"📁 Using persistent Chrome profile: {profile_dir}")
    
    # Set download directory
    download_dir = os.path.join(os.getcwd(), "downloads")
    os.makedirs(download_dir, exist_ok=True)
    
    # Configure download preferences
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True
    }
    chrome_options.add_experimental_option("prefs", prefs)
    
    # Initialize driver (this is the blocking operation - runs in thread pool)
    # Using a shorter implicit wait to speed up initialization
    driver = webdriver.Chrome(options=chrome_options)
    driver.implicitly_wait(5)  # Reduced from 10 to 5 for faster startup
    
    print(f"✅ Chrome WebDriver initialized successfully (debug port: {debug_port})")
    return driver





def get_next_thread_id() -> int:
    """
    Get the next unique thread ID for a browser instance.
    Each browser gets a unique thread ID for tracking and OTP queue management.
    
    Returns:
        int: Unique thread ID for the browser instance
    """
    global thread_counter
    
    with thread_counter_lock:
        thread_counter += 1
        thread_id = thread_counter
    
    # Register this thread in the browser_threads mapping
    with browser_threads_lock:
        browser_threads[thread_id] = {
            "created_at": time.time(),
            "status": "initializing"
        }
    
    return thread_id


def log_thread(thread_id: int, message: str):
    """
    Log a message with thread ID prefix for easy tracking.
    
    Args:
        thread_id: The thread ID of the browser instance
        message: The log message to print
    """
    print(f"[Thread-{thread_id}] {message}")


async def wait_for_otp_from_api(timeout=120, thread_id: Optional[int] = None):
    """
    Wait for OTP to be sent via API endpoint (async version).
    Uses queue-based system for multi-browser support with proper FIFO ordering.
    
    Args:
        timeout: Maximum time to wait for OTP in seconds (default: 120)
        thread_id: Thread ID of the browser instance waiting for OTP.
                   If provided, uses queue-based FIFO distribution.
                   If None, falls back to legacy global storage.
    
    Returns:
        str or None: The OTP code if received, None if timeout
        
    Note:
        - In multi-browser mode (thread_id provided), waits for OTP from queue (FIFO)
        - Uses blocking queue.get() with timeout to ensure proper FIFO ordering
        - In single-request mode (thread_id is None), uses legacy global otp_storage
        - This is async to avoid blocking the server while waiting for OTP
    """
    if thread_id is not None:
        # Multi-browser mode: use queue-based FIFO distribution
        # Register this thread as waiting for OTP (ensures FIFO order)
        with otp_waiting_lock:
            if thread_id not in otp_waiting_threads:
                otp_waiting_threads.append(thread_id)
                wait_position = len(otp_waiting_threads)
            else:
                wait_position = otp_waiting_threads.index(thread_id) + 1
        
        # Update thread status to indicate waiting for OTP
        with browser_threads_lock:
            if thread_id in browser_threads:
                browser_threads[thread_id]["status"] = "waiting_for_otp"
        
        log_thread(thread_id, f"⏳ Waiting for OTP from queue (timeout: {timeout}s, position: {wait_position})...")
        
        # Calculate remaining time for each blocking call
        start_time = time.time()
        remaining_timeout = timeout
        
        # Helper function for blocking queue.get() to ensure proper FIFO ordering
        def get_otp_blocking(timeout_sec: float):
            """Blocking queue.get() - ensures threads wait in FIFO order"""
            return otp_queue.get(timeout=timeout_sec)
        
        while remaining_timeout > 0:
            try:
                # Check if there's an OTP available AND this thread is next in line (FIFO order)
                with otp_waiting_lock:
                    has_otp = otp_queue.qsize() > 0
                    is_my_turn = len(otp_waiting_threads) > 0 and otp_waiting_threads[0] == thread_id
                
                if has_otp and is_my_turn:
                    # This thread is next AND there's an OTP available
                    # Use blocking queue.get() with timeout
                    block_timeout = min(0.3, remaining_timeout)
                    otp_code = await asyncio.to_thread(get_otp_blocking, block_timeout)
                    
                    # IMMEDIATELY remove this thread from waiting list
                    with otp_waiting_lock:
                        if thread_id in otp_waiting_threads:
                            otp_waiting_threads.remove(thread_id)
                    
                    log_thread(thread_id, f"✅ OTP received from queue: {otp_code}")
                    return otp_code
                else:
                    # Either no OTP available OR not this thread's turn yet
                    # Wait a bit and check again
                    await asyncio.sleep(0.1)
                    remaining_timeout = timeout - (time.time() - start_time)
                    if remaining_timeout <= 0:
                        break
                    continue
                    
            except queue.Empty:
                # Timeout on this iteration, check if we should continue
                remaining_timeout = timeout - (time.time() - start_time)
                if remaining_timeout <= 0:
                    break
                # Continue waiting immediately - queue.get() already waited
                continue
            except Exception as e:
                log_thread(thread_id, f"⚠️ Error waiting for OTP: {str(e)}")
                # On error, check remaining time and continue
                remaining_timeout = timeout - (time.time() - start_time)
                if remaining_timeout <= 0:
                    break
                await asyncio.sleep(0.05)  # Very small delay before retry
                continue
        
        # Remove thread from waiting list on timeout
        with otp_waiting_lock:
            if thread_id in otp_waiting_threads:
                otp_waiting_threads.remove(thread_id)
        
        log_thread(thread_id, "❌ OTP timeout - no OTP received from queue within timeout period")
        return None
    else:
        # Legacy single-request mode: use global storage
        print(f"⏳ Waiting for OTP via API endpoint (timeout: {timeout}s)...")
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            if otp_storage["otp"] is not None:
                # Check if OTP is not too old (within 5 minutes)
                if time.time() - otp_storage["timestamp"] < 300:  # 5 minutes
                    otp_code = otp_storage["otp"]
                    otp_storage["otp"] = None  # Clear after use
                    print(f"✅ OTP received: {otp_code}")
                    return otp_code
                else:
                    print("⚠️ OTP expired, clearing...")
                    otp_storage["otp"] = None
            
            # Use asyncio.sleep instead of time.sleep to not block the event loop
            await asyncio.sleep(1)
        
        print("❌ OTP timeout - no OTP received within timeout period")
        return None


def wait_for_otp_sync(timeout: int, thread_id: int):
    """
    Synchronous version of OTP waiting for use in thread pool.
    This function runs in a dedicated thread so blocking operations are OK.
    
    Args:
        timeout: Maximum time to wait for OTP in seconds
        thread_id: Thread ID of the browser instance waiting for OTP
    
    Returns:
        str or None: The OTP code if received, None if timeout
    """
    # Register this thread as waiting for OTP (ensures FIFO order)
    with otp_waiting_lock:
        if thread_id not in otp_waiting_threads:
            otp_waiting_threads.append(thread_id)
            wait_position = len(otp_waiting_threads)
        else:
            wait_position = otp_waiting_threads.index(thread_id) + 1
    
    # Update thread status to indicate waiting for OTP
    with browser_threads_lock:
        if thread_id in browser_threads:
            browser_threads[thread_id]["status"] = "waiting_for_otp"
    
    log_thread(thread_id, f"⏳ Waiting for OTP from queue (timeout: {timeout}s, position: {wait_position})...")
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        # Check if there's an OTP available AND this thread is next in line
        with otp_waiting_lock:
            has_otp = otp_queue.qsize() > 0
            is_my_turn = len(otp_waiting_threads) > 0 and otp_waiting_threads[0] == thread_id
        
        if has_otp and is_my_turn:
            try:
                # Get OTP from queue with short timeout
                otp_code = otp_queue.get(timeout=0.3)
                
                # IMMEDIATELY remove this thread from waiting list
                with otp_waiting_lock:
                    if thread_id in otp_waiting_threads:
                        otp_waiting_threads.remove(thread_id)
                
                log_thread(thread_id, f"✅ OTP received from queue: {otp_code}")
                return otp_code
            except queue.Empty:
                # No OTP available yet, continue waiting
                pass
        
        # Wait a bit before checking again (OK to block - we're in a thread)
        time.sleep(0.1)
    
    # Remove thread from waiting list on timeout
    with otp_waiting_lock:
        if thread_id in otp_waiting_threads:
            otp_waiting_threads.remove(thread_id)
    
    log_thread(thread_id, "❌ OTP timeout - no OTP received from queue within timeout period")
    return None




@app.get("/")
async def root():
    """
    Health check endpoint.
    
    Returns:
        dict: API status information
    """
    return {
        "status": "running",
        "message": "Progressive Driver Add/Update Bot API",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "otp_submit": "POST /otp",
            "otp_status": "GET /otp/status",
            "start": "POST /start"
        }
    }


@app.get("/health")
async def health_check():
    """
    Simple health check endpoint for Railway/monitoring.
    
    Returns:
        dict: Health status
    """
    return {
        "status": "healthy",
        "service": "vehical_replace",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }


@app.get("/otp")
async def otp_info():
    """
    Information about the OTP endpoint (GET request).
    Use POST to submit OTP.
    
    Returns:
        dict: Usage information
    """
    return {
        "message": "OTP Endpoint",
        "method": "POST",
        "usage": "Send POST request with JSON body: {\"otp\": \"123456\"}",
        "current_status": {
            "has_otp": otp_storage["otp"] is not None,
            "age_seconds": int(time.time() - otp_storage["timestamp"]) if otp_storage["timestamp"] else None
        }
    }


def run_automation_sync(request: PolicyRequest, thread_id: int):
    """
    Synchronous automation function that runs in a thread pool.
    This allows multiple browser instances to run concurrently without blocking each other.
    
    Args:
        request: PolicyRequest containing all the request data
        thread_id: Unique thread ID for this browser instance
    
    Returns:
        dict: Success response with automation results
    """
    driver = None
    
    try:
        # -------------------------------------------------------------------------
        # STEP 1: Initialize WebDriver
        # -------------------------------------------------------------------------
        log_thread(thread_id, "🔧 Initializing Chrome WebDriver...")
        driver = setup_chrome_driver()
        
        # Update thread status
        with browser_threads_lock:
            if thread_id in browser_threads:
                browser_threads[thread_id]["status"] = "browser_initialized"
        
        log_thread(thread_id, "✅ Chrome WebDriver initialized successfully")
        
        # -------------------------------------------------------------------------
        # STEP 2: Navigate to Progressive login page
        # -------------------------------------------------------------------------
        log_thread(thread_id, "🌐 Navigating to Progressive login page...")
        login_url = "https://www.foragentsonlylogin.progressive.com/Login/?flowId=5IZypmklew"
        driver.get(login_url)
        
        # Wait for page to load completely
        wait = WebDriverWait(driver, 15)
        time.sleep(3)
        
        # Log current page title for debugging
        log_thread(thread_id, f"Page title: {driver.title}")
        log_thread(thread_id, f"Current URL: {driver.current_url}")
        
        # -------------------------------------------------------------------------
        # STEP 3: Fill in login credentials
        # -------------------------------------------------------------------------
        
        # Wait for username field to be present and interactable
        username_field = wait.until(
            EC.presence_of_element_located((By.ID, "user1"))
        )
        username_field.clear()
        username_field.send_keys(request.username)
        log_thread(thread_id, f"Username entered: {request.username}")
        
        # Wait for password field to be present and interactable
        password_field = wait.until(
            EC.presence_of_element_located((By.ID, "password1"))
        )
        password_field.clear()
        password_field.send_keys(request.password)
        log_thread(thread_id, "Password entered")
        
        # Wait a moment before clicking login
        time.sleep(1)
        
        # Click the login button
        login_button = wait.until(
            EC.element_to_be_clickable((By.ID, "image1"))
        )
        login_button.click()
        log_thread(thread_id, "Login button clicked")
        
        # Wait for login to process and page to load
        time.sleep(8)
        
        log_thread(thread_id, f"After login - Page title: {driver.title}")
        log_thread(thread_id, f"After login - Current URL: {driver.current_url}")
        
        # Check if we're on the expected page or if MFA is required
        page_source_snippet = driver.page_source[:500]
        print(f"Page source preview: {page_source_snippet}")
        
        # -------------------------------------------------------------------------
        # STEP 3.1: Handle OTP (MFA) if present
        # -------------------------------------------------------------------------
        try:
            # Check for OTP field after login (with timeout)
            print("Checking for OTP field...")
            otp_field_found = False
            
            try:
                # Wait for OTP field with shorter timeout
                otp_field = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.ID, "reauth-sms-otp-input"))
                )
                log_thread(thread_id, "✅ OTP field found - waiting for OTP...")
                otp_field_found = True
                
                # Wait for OTP to be entered via API endpoint (synchronous - we're in a thread pool)
                # Pass thread_id for queue-based OTP distribution
                otp_code = wait_for_otp_sync(timeout=120, thread_id=thread_id)
                
                if otp_code:
                    log_thread(thread_id, f"✅ Received OTP: {otp_code}")
                    
                    # Wait for OTP field to be clickable and clear it
                    WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable((By.ID, "reauth-sms-otp-input"))
                    )
                    otp_field.clear()
                    time.sleep(0.5)
                    
                    # Enter OTP using JavaScript to ensure it works
                    driver.execute_script("arguments[0].value = '';", otp_field)
                    driver.execute_script("arguments[0].value = arguments[1];", otp_field, otp_code)
                    driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", otp_field)
                    driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", otp_field)
                    log_thread(thread_id, f"✅ Entered OTP: {otp_code}")
                    
                    # Wait a moment for the field to register the input
                    time.sleep(2)
                    
                    # Verify we're on the correct URL before clicking Continue button
                    current_url = driver.current_url
                    print(f"📍 Current URL before clicking Continue: {current_url}")
                    
                    # Check if we're on the correct Progressive login page
                    if "foragentsonlylogin.progressive.com" not in current_url:
                        print("❌ Not on correct Progressive login page!")
                        print(f"Expected: foragentsonlylogin.progressive.com")
                        print(f"Actual: {current_url}")
                        raise Exception("Bot is not on the correct Progressive login page")
                    
                    print("✅ Confirmed on correct Progressive login page")
                    
                    # Find and click Continue button - SIMPLE METHOD like login button
                    print("🔍 Looking for Continue button...")
                    
                    # Take screenshot before clicking
                    driver.save_screenshot("before_continue_click.png")
                    print("📸 Screenshot saved: before_continue_click.png")
                    
                    try:
                        print("🔍 Looking for Continue button...")
                        
                        # First, let's check what elements are actually present
                        print("🔍 Checking what elements are available...")
                        try:
                            all_buttons = driver.find_elements(By.TAG_NAME, "button")
                            print(f"📊 Found {len(all_buttons)} buttons on page")
                            for i, btn in enumerate(all_buttons[:5]):  # Show first 5 buttons
                                try:
                                    btn_class = btn.get_attribute("class")
                                    btn_text = btn.text
                                    print(f"   Button {i+1}: class='{btn_class}', text='{btn_text}'")
                                except:
                                    print(f"   Button {i+1}: Could not get details")
                        except Exception as e:
                            print(f"⚠️ Could not list buttons: {e}")
                        
                        # Wait for the Continue button to become visible and interactable
                        print("⏳ Waiting for Continue button to become visible...")
                        continue_button = None
                        
                        try:
                            # Wait up to 10 seconds for the button to become visible
                            continue_button = WebDriverWait(driver, 10).until(
                                EC.element_to_be_clickable((By.XPATH, "//button[@class='base-btn js-mfa-reauth-submit-button' and text()='Continue']"))
                            )
                            print("✅ Found VISIBLE Continue button after waiting!")
                        except Exception as e:
                            print(f"⚠️ Wait for visible button failed: {e}")
                            
                            # Fallback: Try to find the SPECIFIC visible Continue button
                            print("🔄 Trying fallback method - finding SPECIFIC visible Continue button...")
                            try:
                                # Get all Continue buttons and find the visible one
                                all_continue_buttons = driver.find_elements(By.XPATH, "//button[text()='Continue']")
                                print(f"📊 Found {len(all_continue_buttons)} Continue buttons")
                                
                                for i, btn in enumerate(all_continue_buttons):
                                    try:
                                        btn_text = btn.text
                                        btn_displayed = btn.is_displayed()
                                        btn_enabled = btn.is_enabled()
                                        print(f"   Continue Button {i+1}: text='{btn_text}', displayed={btn_displayed}, enabled={btn_enabled}")
                                        
                                        if btn_displayed and btn_enabled and btn_text.strip() == 'Continue':
                                            continue_button = btn
                                            print(f"✅ Found VISIBLE Continue button (Button {i+1})")
                                            break
                                    except Exception as btn_e:
                                        print(f"   Continue Button {i+1}: Error getting details - {btn_e}")
                                
                                if not continue_button:
                                    raise Exception("No visible Continue button found")
                                    
                            except Exception as e2:
                                print(f"❌ Fallback also failed: {e2}")
                                raise Exception("Could not find Continue button with any method")
                        
                        if not continue_button:
                            raise Exception("Could not find Continue button with any selector")
                        
                        print("✅ Continue button found!")
                        
                        # Get button properties for debugging
                        button_text = continue_button.text
                        button_enabled = continue_button.is_enabled()
                        button_displayed = continue_button.is_displayed()
                        button_class = continue_button.get_attribute("class")
                        print(f"🔍 Button info - Text: '{button_text}', Enabled: {button_enabled}, Displayed: {button_displayed}, Class: '{button_class}'")
                        
                        # Try multiple clicking methods to ensure it works
                        success = False
                        
                        # Method 1: JavaScript click (most reliable)
                        try:
                            print("🔄 Trying JavaScript click (most reliable)...")
                            driver.execute_script("arguments[0].click();", continue_button)
                            print("✅ JavaScript click successful!")
                            
                            # Verify if click worked
                            time.sleep(2)
                            try:
                                driver.find_element(By.ID, "reauth-sms-otp-input")
                                print("❌ JavaScript click didn't work - OTP field still present")
                            except:
                                print("✅ JavaScript click worked - OTP field gone!")
                                success = True
                        except Exception as e1:
                            print(f"⚠️ JavaScript click failed: {e1}")
                        
                        # Method 2: Simple click like login button
                        if not success:
                            try:
                                print("🔄 Trying simple click (like login button)...")
                                continue_button.click()
                                print("✅ Simple click successful!")
                                
                                # Verify if click worked
                                time.sleep(2)
                                try:
                                    driver.find_element(By.ID, "reauth-sms-otp-input")
                                    print("❌ Simple click didn't work - OTP field still present")
                                except:
                                    print("✅ Simple click worked - OTP field gone!")
                                    success = True
                            except Exception as e2:
                                print(f"⚠️ Simple click failed: {e2}")
                        
                        # Method 3: Form submission
                        if not success:
                            try:
                                print("🔄 Trying form submission...")
                                form = driver.find_element(By.CSS_SELECTOR, "form.js-mfa-reauth-sms-otp")
                                driver.execute_script("arguments[0].submit();", form)
                                
                                # Verify if submission worked
                                time.sleep(2)
                                try:
                                    driver.find_element(By.ID, "reauth-sms-otp-input")
                                    print("❌ Form submission didn't work - OTP field still present")
                                except:
                                    print("✅ Form submission worked - OTP field gone!")
                                    success = True
                            except Exception as e3:
                                print(f"⚠️ Form submission failed: {e3}")
                        
                        # Method 4: Force click with JavaScript
                        if not success:
                            try:
                                print("🔄 Trying force click...")
                                force_button = driver.find_element(By.CSS_SELECTOR, "button.base-btn.js-mfa-reauth-submit-button")
                                driver.execute_script("""
                                    var button = arguments[0];
                                    button.style.pointerEvents = 'auto';
                                    button.style.display = 'block';
                                    button.style.visibility = 'visible';
                                    button.click();
                                """, force_button)
                                
                                # Verify if click worked
                                time.sleep(2)
                                try:
                                    driver.find_element(By.ID, "reauth-sms-otp-input")
                                    print("❌ Force click didn't work - OTP field still present")
                                except:
                                    print("✅ Force click worked - OTP field gone!")
                                    success = True
                            except Exception as e4:
                                print(f"⚠️ Force click failed: {e4}")
                        
                        # Method 5: Direct form submission
                        if not success:
                            try:
                                print("🔄 Trying direct form submission...")
                                driver.execute_script("""
                                    // Find the form and submit it directly
                                    var forms = document.getElementsByTagName('form');
                                    for (var i = 0; i < forms.length; i++) {
                                        if (forms[i].querySelector('input[id="reauth-sms-otp-input"]')) {
                                            forms[i].submit();
                                            break;
                                        }
                                    }
                                """)
                                print("✅ Submitted form directly!")
                                
                                # Wait a moment for the form to process
                                time.sleep(2)
                                
                                # Check if OTP field disappeared (indicating success)
                                try:
                                    WebDriverWait(driver, 3).until(
                                        EC.invisibility_of_element_located((By.ID, "reauth-sms-otp-input"))
                                    )
                                    print("✅ OTP field disappeared - form submission successful!")
                                    success = True
                                except:
                                    print("⚠️ OTP field still present after form submission")
                            except Exception as e3:
                                print(f"⚠️ Direct form submission failed: {e3}")
                        
                        # Method 6: Last resort - Press Enter key
                        if not success:
                            try:
                                print("🔄 Trying Enter key press...")
                                otp_field = driver.find_element(By.ID, "reauth-sms-otp-input")
                                otp_field.send_keys(Keys.RETURN)
                                
                                # Verify if Enter key worked
                                time.sleep(2)
                                try:
                                    driver.find_element(By.ID, "reauth-sms-otp-input")
                                    print("❌ Enter key didn't work - OTP field still present")
                                except:
                                    print("✅ Enter key worked - OTP field gone!")
                                    success = True
                            except Exception as e6:
                                print(f"⚠️ Enter key failed: {e6}")
                        
                        if not success:
                            print("❌ ALL METHODS FAILED - Trying manual form submission...")
                            # Last resort: Manual form submission
                            try:
                                driver.execute_script("""
                                    // Find the OTP form and submit it manually
                                    var forms = document.getElementsByTagName('form');
                                    for (var i = 0; i < forms.length; i++) {
                                        var form = forms[i];
                                        if (form.querySelector('input[id="reauth-sms-otp-input"]')) {
                                            // Create a submit event
                                            var submitEvent = new Event('submit', {
                                                bubbles: true,
                                                cancelable: true
                                            });
                                            form.dispatchEvent(submitEvent);
                                            
                                            // Also try direct submission
                                            form.submit();
                                            break;
                                        }
                                    }
                                """)
                                print("✅ Manual form submission attempted")
                                success = True  # Assume it worked
                            except Exception as e7:
                                print(f"❌ Manual form submission failed: {e7}")
                        
                        # Verify the click actually worked by checking if page changed
                        print("🔍 Verifying if Continue button click worked...")
                        time.sleep(3)
                        
                        # Check if we're still on the same page (OTP page)
                        current_url_after = driver.current_url
                        print(f"📍 Current URL after click: {current_url_after}")
                        
                        # Check if URL changed (indicates successful navigation)
                        if current_url_after != current_url:
                            print("✅ URL changed - Continue button click worked!")
                            success = True
                        else:
                            print("❌ URL didn't change - click didn't work!")
                        
                        # Also check if OTP field still exists
                        try:
                            otp_field_check = driver.find_element(By.ID, "reauth-sms-otp-input")
                            print("❌ OTP field still present - click didn't work!")
                            
                            if not success:
                                # Try the most aggressive method - direct form submission
                                print("🔄 Trying direct form submission...")
                                driver.execute_script("""
                                    // Find the form and submit it directly
                                    var forms = document.getElementsByTagName('form');
                                    for (var i = 0; i < forms.length; i++) {
                                        if (forms[i].querySelector('input[id="reauth-sms-otp-input"]')) {
                                            forms[i].submit();
                                            break;
                                        }
                                    }
                                """)
                                print("✅ Submitted form directly!")
                                time.sleep(3)
                                
                                # Check URL again after form submission
                                final_url = driver.current_url
                                print(f"📍 Final URL after form submission: {final_url}")
                                
                        except:
                            print("✅ OTP field no longer present - click worked!")
                            success = True
                        
                        # Wait for page to process the OTP
                        time.sleep(5)
                        print("✅ OTP verification completed - waiting for page to load...")
                        
                        # Take screenshot after clicking
                        driver.save_screenshot("after_continue_click.png")
                        print("📸 Screenshot saved: after_continue_click.png")
                        
                    except Exception as e2:
                        print(f"❌ Error clicking Continue button: {e2}")
                        print("❌ All clicking methods failed - OTP verification incomplete")
                        raise Exception("Could not click Continue button with any method")
                else:
                    print("❌ No OTP received from API")
                    raise Exception("OTP not received from API")
                    
            except Exception as e:
                print(f"⚠️ OTP field not found (timeout after 5 seconds): {e}")
                print("✅ No OTP required - continuing with normal flow...")
                otp_field_found = False
            
            # If OTP field was not found, continue with normal flow
            if not otp_field_found:
                print("🔄 Proceeding with normal login flow (no OTP required)")
                time.sleep(2)  # Brief wait for page to settle
            
            print("Login completed!")
        except Exception as e:
            print(f"❌ Error in OTP handling: {e}")
            print("🔄 Continuing with normal flow...")
        
        # -------------------------------------------------------------------------
        # STEP 4: Select Policy search option and enter policy number
        # -------------------------------------------------------------------------
        
        # Increase wait time for elements
        extended_wait = WebDriverWait(driver, 30)
        
        # Wait for the policy search radio button to be available and click it
        print("Waiting for policy search radio button...")
        
        try:
            # First, wait for the radio button to be present in the DOM
            policy_radio_button = extended_wait.until(
                EC.presence_of_element_located((By.ID, "SBP_PolSearch"))
            )
            print(f"Policy radio button found: {policy_radio_button}")
            
            # Check if it's already selected
            is_selected = policy_radio_button.is_selected()
            print(f"Radio button already selected: {is_selected}")
            
            if not is_selected:
                # Scroll to element to ensure it's visible
                driver.execute_script("arguments[0].scrollIntoView(true);", policy_radio_button)
                time.sleep(1)
                
                # Try clicking using JavaScript (most reliable for radio buttons)
                driver.execute_script("arguments[0].click();", policy_radio_button)
                print("Policy search radio button clicked using JavaScript")
                
                # Verify it was selected
                time.sleep(0.5)
                is_selected = policy_radio_button.is_selected()
                print(f"Radio button now selected: {is_selected}")
            else:
                print("Radio button already selected, skipping click")
                
        except Exception as e:
            print(f"Error clicking policy radio button: {str(e)}")
            # Try alternative approach - click the label
            try:
                label = driver.find_element(By.CSS_SELECTOR, "label[for='SBP_PolSearch']")
                driver.execute_script("arguments[0].click();", label)
                print("Clicked policy radio button via label")
            except Exception as label_error:
                print(f"Label click also failed: {str(label_error)}")
                raise
        
        # Wait a moment for the input field to become active
        time.sleep(2)
        
        # Wait for the policy number input field and enter the policy number
        print("Waiting for policy number input field...")
        policy_input_field = extended_wait.until(
            EC.presence_of_element_located((By.ID, "SBP_UserSelectedPol"))
        )
        policy_input_field.clear()
        policy_input_field.send_keys(request.policy_no)
        print(f"Policy number entered: {request.policy_no}")
        
        # Wait a moment before clicking search
        time.sleep(1)
        
        # -------------------------------------------------------------------------
        # STEP 5: Click the Search button
        # -------------------------------------------------------------------------
        
        print("Waiting for search button...")
        search_button = extended_wait.until(
            EC.element_to_be_clickable((By.ID, "sbp-search"))
        )
        
        # Scroll to search button to ensure it's visible
        driver.execute_script("arguments[0].scrollIntoView(true);", search_button)
        time.sleep(0.5)
        
        # Click the search button
        search_button.click()
        print("Search button clicked")
   
        # Wait for search results page to load completely
        time.sleep(8)
        
        print(f"After search - Title: {driver.title}")
        print(f"After search - URL: {driver.current_url}")
        
        # -------------------------------------------------------------------------
        # STEP 6: Find and click the policy button matching the policy number
        # -------------------------------------------------------------------------
        
        print(f"Looking for policy button with policy number: {request.policy_no}")
        
        # Wait for policy buttons to be present
        time.sleep(3)
        
        # Build the text to search for (format: "Auto {policy_no}")
        policy_text = f"Auto {request.policy_no}"
        print(f"Searching for button containing text: {policy_text}")
        
        # Find the button that contains the specific policy number
        # Using XPath to find span with the policy text, then get its parent button
        try:
            policy_button = extended_wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, f"//span[contains(text(), 'Auto {request.policy_no}')]/ancestor::button")
                )
            )
            print(f"Found policy button: {policy_button.get_attribute('title')}")
            
            # Scroll to the button
            driver.execute_script("arguments[0].scrollIntoView(true);", policy_button)
            time.sleep(1)
            
            # Click the policy button
            driver.execute_script("arguments[0].click();", policy_button)
            print(f"Clicked policy button for: {policy_text}")
            
            # Wait for policy details slider to load
            time.sleep(5)
            
            print(f"After clicking policy - Title: {driver.title}")
            print(f"After clicking policy - URL: {driver.current_url}")
            
        except TimeoutException:
            print(f"Could not find policy button for policy number: {request.policy_no}")
            raise HTTPException(
                status_code=404,
                detail=f"Policy number {request.policy_no} not found in search results"
            )
        
        # -------------------------------------------------------------------------
        # STEP 7: Click on "Drivers" button from the dropdown menu
        # -------------------------------------------------------------------------
        
        print("Looking for 'Drivers' button in dropdown menu...")
        
        try:
            # Find the Drivers button by looking for the paragraph tag with "Drivers" text
            # The clickable element is the parent div
            drivers_button = extended_wait.until(
                EC.element_to_be_clickable((By.XPATH, "//p[contains(text(), 'Drivers')]/ancestor::div[@class='flex pv1 items-center w-100 ng-star-inserted']"))
            )
            print("Found 'Drivers' button in dropdown")
            
            # Scroll to the button
            driver.execute_script("arguments[0].scrollIntoView(true);", drivers_button)
            time.sleep(1)
            
            # Click the Drivers button
            driver.execute_script("arguments[0].click();", drivers_button)
            print("Clicked 'Drivers' button")
            
            # Wait for the drivers section/sub-panel to load
            time.sleep(5)
            
            print(f"After Drivers click - Title: {driver.title}")
            print(f"After Drivers click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find 'Drivers' button in dropdown")
            raise HTTPException(
                status_code=404,
                detail="Drivers button not found in dropdown menu"
            )
        
        # -------------------------------------------------------------------------
        # STEP 8: Click on action button based on action_type (Add Driver, Update Driver, or Vehicle actions)
        # -------------------------------------------------------------------------
        
        action_type_lower = request.action_type.lower()
        
        if "add" in action_type_lower and "driver" in action_type_lower:
            # Handle "Add Driver" action
            print("Looking for 'Add Driver' option in second dropdown...")
            
            try:
                # Find the "Add Driver" link by its data-pgr-id attribute
                add_driver_button = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "a[data-pgr-id='btnAddDriver']"))
                )
                print("Found 'Add Driver' option")
                
                # Scroll to the button
                driver.execute_script("arguments[0].scrollIntoView(true);", add_driver_button)
                time.sleep(1)
                
                # Click the Add Driver button
                driver.execute_script("arguments[0].click();", add_driver_button)
                print("Clicked 'Add Driver' option")
                
                # Wait for the add driver page to load
                time.sleep(5)
                
                print(f"After Add Driver click - Title: {driver.title}")
                print(f"After Add Driver click - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find 'Add Driver' option")
                raise HTTPException(
                    status_code=404,
                    detail="Add Driver option not found in second dropdown"
                )
        elif "update" in action_type_lower and "driver" in action_type_lower:
            # Handle "Update Driver" action
            print("Looking for 'Update Driver' option in second dropdown...")
            
            try:
                # Find the "Update Driver" link by its data-pgr-id attribute
                update_driver_button = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "a[data-pgr-id='btnUpdateDriver']"))
                )
                print("Found 'Update Driver' option")
                
                # Scroll to the button
                driver.execute_script("arguments[0].scrollIntoView(true);", update_driver_button)
                time.sleep(1)
                
                # Click the Update Driver button
                driver.execute_script("arguments[0].click();", update_driver_button)
                print("Clicked 'Update Driver' option")
                
                # Wait for the update driver page to load
                time.sleep(5)
                
                print(f"After Update Driver click - Title: {driver.title}")
                print(f"After Update Driver click - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find 'Update Driver' option")
                raise HTTPException(
                    status_code=404,
                    detail="Update Driver option not found in second dropdown"
                )
        elif "replace" in action_type_lower:
            # Handle "Replace Vehicle" action
            print("Looking for 'Replace Vehicle' option in second dropdown...")
            
            try:
                # Find the "Replace Vehicle" link by its data-pgr-id attribute
                replace_vehicle_button = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "a[data-pgr-id='btnReplaceVehicle']"))
                )
                print("Found 'Replace Vehicle' option")
                
                # Scroll to the button
                driver.execute_script("arguments[0].scrollIntoView(true);", replace_vehicle_button)
                time.sleep(1)
                
                # Click the Replace Vehicle button
                driver.execute_script("arguments[0].click();", replace_vehicle_button)
                print("Clicked 'Replace Vehicle' option")
                
                # Wait for the replace vehicle page to load
                time.sleep(5)
                
                print(f"After Replace Vehicle click - Title: {driver.title}")
                print(f"After Replace Vehicle click - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find 'Replace Vehicle' option")
                raise HTTPException(
                    status_code=404,
                    detail="Replace Vehicle option not found in second dropdown"
                )
                
        elif "add" in action_type_lower:
            # Handle "Add a Vehicle" action
            print("Looking for 'Add a Vehicle' option in second dropdown...")
            
            try:
                # Find the "Add a Vehicle" link by its data-pgr-id attribute
                add_vehicle_button = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "a[data-pgr-id='btnAddaVehicle']"))
                )
                print("Found 'Add a Vehicle' option")
                
                # Scroll to the button
                driver.execute_script("arguments[0].scrollIntoView(true);", add_vehicle_button)
                time.sleep(1)
                
                # Click the Add a Vehicle button
                driver.execute_script("arguments[0].click();", add_vehicle_button)
                print("Clicked 'Add a Vehicle' option")
                
                # Wait for the add vehicle page to load
                time.sleep(5)
                
                print(f"After Add a Vehicle click - Title: {driver.title}")
                print(f"After Add a Vehicle click - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find 'Add a Vehicle' option")
                raise HTTPException(
                    status_code=404,
                    detail="Add a Vehicle option not found in second dropdown"
                )
        else:
            # Invalid action_type
            print(f"Invalid action_type: {request.action_type}")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid action_type: '{request.action_type}'. Must be 'add driver', 'add vehical', or 'replace vehical'"
            )
        
        # -------------------------------------------------------------------------
        # STEP 9: Enter the date from payload (date_to_add_driver for driver actions, date_to_rep_vehical for vehicle actions)
        # -------------------------------------------------------------------------
        
        print("Looking for date input field...")
        
        # Determine which date field to use based on action type
        if "driver" in action_type_lower:
            # Use date_to_add_driver for both "add driver" and "update driver" actions
            date_to_enter = request.date_to_add_driver
            print(f"Date to enter (driver action): {date_to_enter}")
        else:
            date_to_enter = request.date_to_rep_vehical
            print(f"Date to enter (vehicle): {date_to_enter}")
        
        try:
            # Find the date input field by its data-pgr-id attribute
            date_input_field = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-pgr-id='txtChangeEffectiveDate']"))
            )
            print("Found effective date input field")
            
            # Scroll to the input field
            driver.execute_script("arguments[0].scrollIntoView(true);", date_input_field)
            time.sleep(1)
            
            # Clear the field first
            date_input_field.clear()
            time.sleep(0.5)
            
            # Enter the date from payload
            date_input_field.send_keys(date_to_enter)
            if "driver" in action_type_lower:
                print(f"Entered date for driver action: {date_to_enter}")
            else:
                print(f"Entered date for vehicle action: {date_to_enter}")
            
            # Trigger change events to ensure the field registers the input
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", date_input_field)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", date_input_field)
            
            # Wait a moment for the field to register
            time.sleep(2)
            
            print(f"After date entry - Title: {driver.title}")
            print(f"After date entry - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find effective date input field")
            if "driver" in action_type_lower:
                raise HTTPException(
                    status_code=404,
                    detail="Effective date input field not found on driver page"
                )
            else:
                raise HTTPException(
                    status_code=404,
                    detail="Effective date input field not found on vehicle page"
                )
        
        # -------------------------------------------------------------------------
        # STEP 10: Select the first option from the requester type dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for requester type dropdown...")
        
        try:
            # Find the dropdown by its data-pgr-id attribute
            requester_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlTranRequesterTypeCode']"))
            )
            print("Found requester type dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", requester_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", requester_dropdown)
            driver.execute_script("arguments[0].click();", requester_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events (more reliable for Angular)
            first_option_value = "I~Quoc Ho"  # First option value
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, requester_dropdown, first_option_value)
            
            print(f"Selected first option: Named insured - Quoc Ho (value: {first_option_value})")
            
            # Verify selection
            selected_value = requester_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After dropdown selection - Title: {driver.title}")
            print(f"After dropdown selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find requester type dropdown")
            raise HTTPException(
                status_code=404,
                detail="Requester type dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 11: Enter the agent contact name
        # -------------------------------------------------------------------------
        
        print("Looking for agent contact name input field...")
        print(f"Agent name to enter: {request.agent_name}")
        
        try:
            # Find the agent contact name input field by its data-pgr-id attribute
            agent_name_field = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-pgr-id='txtAgencyContactName']"))
            )
            print("Found agent contact name input field")
            
            # Scroll to the input field
            driver.execute_script("arguments[0].scrollIntoView(true);", agent_name_field)
            time.sleep(1)
            
            # Use JavaScript to set the value directly to avoid stale element issues
            driver.execute_script("""
                var input = document.querySelector("input[data-pgr-id='txtAgencyContactName']");
                if (input) {
                    input.value = arguments[0];
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.dispatchEvent(new Event('blur', { bubbles: true }));
                }
            """, request.agent_name)
            print(f"Entered agent name: {request.agent_name}")
            
            # Wait a moment for the field to register
            time.sleep(2)
            
            print(f"After agent name entry - Title: {driver.title}")
            print(f"After agent name entry - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find agent contact name input field")
            raise HTTPException(
                status_code=404,
                detail="Agent contact name input field not found"
            )
        except Exception as e:
            print(f"Error entering agent name: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to enter agent name: {str(e)}"
            )
        
        # -------------------------------------------------------------------------
        # STEP 12: Select the first option from agent email address dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for agent email address dropdown...")
        
        try:
            # Find the dropdown by its data-pgr-id attribute
            agent_email_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlSelectERDAgentEmailAddress']"))
            )
            print("Found agent email address dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", agent_email_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", agent_email_dropdown)
            driver.execute_script("arguments[0].click();", agent_email_dropdown)
            time.sleep(1)
            
            # Select the first non-empty option (index 1, since index 0 is empty)
            select = Select(agent_email_dropdown)
            select.select_by_index(1)
            
            # Get the selected option text for logging
            selected_option = select.first_selected_option
            selected_text = selected_option.text.strip()
            selected_value = selected_option.get_attribute('value')
            print(f"Selected first option: {selected_text} (value: {selected_value})")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After email dropdown selection - Title: {driver.title}")
            print(f"After email dropdown selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find agent email address dropdown")
            raise HTTPException(
                status_code=404,
                detail="Agent email address dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 13: Click the "Continue" button
        # -------------------------------------------------------------------------
        
        print("Looking for 'Continue' button...")
        
        try:
            # Find the Continue button by its data-pgr-id attribute
            continue_button = extended_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
            )
            print("Found 'Continue' button")
            
            # Scroll to the button
            driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
            time.sleep(1)
            
            # Click the Continue button
            driver.execute_script("arguments[0].click();", continue_button)
            print("Clicked 'Continue' button")
            
            # Wait for the new page to load
            time.sleep(5)
            
            print(f"After Continue click - Title: {driver.title}")
            print(f"After Continue click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find 'Continue' button")
            raise HTTPException(
                status_code=404,
                detail="Continue button not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 14: Enter driver first name (for driver actions) OR Find and select vehicle (for replace vehicle action)
        # -------------------------------------------------------------------------
        
        if "driver" in action_type_lower:
            # Handle driver actions - enter driver first name
            print("Looking for driver first name input field...")
            print(f"Driver first name to enter: {request.driver_first_name}")
            
            try:
                # Find the driver first name input field by its data-pgr-id attribute
                driver_first_name_field = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-pgr-id='txtDriverFirstName']"))
                )
                print("Found driver first name input field")
                
                # Scroll to the input field
                driver.execute_script("arguments[0].scrollIntoView(true);", driver_first_name_field)
                time.sleep(1)
                
                # Clear the field first
                driver_first_name_field.clear()
                time.sleep(0.5)
                
                # Enter the driver first name from payload
                driver_first_name_field.send_keys(request.driver_first_name)
                print(f"Entered driver first name: {request.driver_first_name}")
                
                # Trigger change events to ensure the field registers the input
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", driver_first_name_field)
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", driver_first_name_field)
                
                # Wait a moment for the field to register
                time.sleep(2)
                
                print(f"After driver first name entry - Title: {driver.title}")
                print(f"After driver first name entry - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver first name input field")
                raise HTTPException(
                    status_code=404,
                    detail="Driver first name input field not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 15: Enter driver last name (for driver actions)
            # -------------------------------------------------------------------------
            
            print("Looking for driver last name input field...")
            print(f"Driver last name to enter: {request.driver_last_name}")
            
            try:
                # Find the driver last name input field by its data-pgr-id attribute
                driver_last_name_field = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-pgr-id='txtDriverLastName']"))
                )
                print("Found driver last name input field")
                
                # Scroll to the input field
                driver.execute_script("arguments[0].scrollIntoView(true);", driver_last_name_field)
                time.sleep(1)
                
                # Clear the field first
                driver_last_name_field.clear()
                time.sleep(0.5)
                
                # Enter the driver last name from payload
                driver_last_name_field.send_keys(request.driver_last_name)
                print(f"Entered driver last name: {request.driver_last_name}")
                
                # Trigger change events to ensure the field registers the input
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", driver_last_name_field)
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", driver_last_name_field)
                
                # Wait a moment for the field to register
                time.sleep(2)
                
                print(f"After driver last name entry - Title: {driver.title}")
                print(f"After driver last name entry - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver last name input field")
                raise HTTPException(
                    status_code=404,
                    detail="Driver last name input field not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 16: Enter driver date of birth (for driver actions)
            # -------------------------------------------------------------------------
            
            print("Looking for driver date of birth input field...")
            print(f"Driver DOB to enter: {request.driver_dob}")
            
            try:
                # Find the driver DOB input field by its data-pgr-id attribute
                driver_dob_field = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-pgr-id='txtDriverDOB']"))
                )
                print("Found driver date of birth input field")
                
                # Scroll to the input field
                driver.execute_script("arguments[0].scrollIntoView(true);", driver_dob_field)
                time.sleep(1)
                
                # Clear the field first
                driver_dob_field.clear()
                time.sleep(0.5)
                
                # Enter the driver DOB from payload (format: mm/dd/yyyy)
                driver_dob_field.send_keys(request.driver_dob)
                print(f"Entered driver date of birth: {request.driver_dob}")
                
                # Trigger change events to ensure the field registers the input
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", driver_dob_field)
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", driver_dob_field)
                
                # Wait a moment for the field to register
                time.sleep(2)
                
                print(f"After driver DOB entry - Title: {driver.title}")
                print(f"After driver DOB entry - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver date of birth input field")
                raise HTTPException(
                    status_code=404,
                    detail="Driver date of birth input field not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 17: Select driver gender (Male or Female) based on payload
            # -------------------------------------------------------------------------
            
            print("Looking for driver gender radio buttons...")
            driver_gender_lower = request.driver_gender.lower()
            print(f"Driver gender to select: {request.driver_gender}")
            
            try:
                if "male" in driver_gender_lower or driver_gender_lower == "m":
                    # Select Male radio button
                    print("Selecting Male gender...")
                    male_radio = extended_wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-pgr-id='radDriverSex60'][value='M']"))
                    )
                    print("Found Male radio button")
                    
                    # Scroll to the radio button
                    driver.execute_script("arguments[0].scrollIntoView(true);", male_radio)
                    time.sleep(1)
                    
                    # Click the Male radio button
                    driver.execute_script("arguments[0].click();", male_radio)
                    print("Selected Male gender")
                    
                    # Wait a moment for the selection to register
                    time.sleep(2)
                    
                else:
                    # Select Female radio button (default if not male)
                    print("Selecting Female gender...")
                    female_radio = extended_wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-pgr-id='radDriverSex60'][value='F']"))
                    )
                    print("Found Female radio button")
                    
                    # Scroll to the radio button
                    driver.execute_script("arguments[0].scrollIntoView(true);", female_radio)
                    time.sleep(1)
                    
                    # Click the Female radio button
                    driver.execute_script("arguments[0].click();", female_radio)
                    print("Selected Female gender")
                    
                    # Wait a moment for the selection to register
                    time.sleep(2)
                
                print(f"After gender selection - Title: {driver.title}")
                print(f"After gender selection - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver gender radio buttons")
                raise HTTPException(
                    status_code=404,
                    detail="Driver gender radio buttons not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 18: Select driver marital status (Married or Single) based on payload
            # -------------------------------------------------------------------------
            
            print("Looking for driver marital status radio buttons...")
            driver_marital_status_lower = request.driver_marital_status.lower()
            print(f"Driver marital status to select: {request.driver_marital_status}")
            
            try:
                if "married" in driver_marital_status_lower or driver_marital_status_lower == "m":
                    # Select Married radio button
                    print("Selecting Married marital status...")
                    married_radio = extended_wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-pgr-id='radDriverMaritalStatus70'][value='M']"))
                    )
                    print("Found Married radio button")
                    
                    # Scroll to the radio button
                    driver.execute_script("arguments[0].scrollIntoView(true);", married_radio)
                    time.sleep(1)
                    
                    # Click the Married radio button
                    driver.execute_script("arguments[0].click();", married_radio)
                    print("Selected Married marital status")
                    
                    # Wait a moment for the selection to register
                    time.sleep(2)
                    
                else:
                    # Select Single radio button (default if not married)
                    print("Selecting Single marital status...")
                    single_radio = extended_wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-pgr-id='radDriverMaritalStatus70'][value='S']"))
                    )
                    print("Found Single radio button")
                    
                    # Scroll to the radio button
                    driver.execute_script("arguments[0].scrollIntoView(true);", single_radio)
                    time.sleep(1)
                    
                    # Click the Single radio button
                    driver.execute_script("arguments[0].click();", single_radio)
                    print("Selected Single marital status")
                    
                    # Wait a moment for the selection to register
                    time.sleep(2)
                
                print(f"After marital status selection - Title: {driver.title}")
                print(f"After marital status selection - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver marital status radio buttons")
                raise HTTPException(
                    status_code=404,
                    detail="Driver marital status radio buttons not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 19: Select "Other relation" from driver relationship dropdown
            # -------------------------------------------------------------------------
            
            print("Looking for driver relationship dropdown...")
            
            try:
                # Wait for the dropdown to be present
                extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlDriverRelationship']"))
                )
                print("Found driver relationship dropdown")
                
                # Use JavaScript to find, scroll, focus, click, and select - all in one to avoid stale element references
                other_relation_value = "O"
                driver.execute_script("""
                    var select = document.querySelector("select[data-pgr-id='ddlDriverRelationship']");
                    if (select) {
                        // Scroll to the dropdown
                        select.scrollIntoView({block: 'center', behavior: 'smooth'});
                        
                        // Focus and click to open the dropdown
                        select.focus();
                        select.click();
                        
                        // Set the value
                        select.value = arguments[0];
                        
                        // Dispatch events to ensure the change is registered
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                        select.dispatchEvent(new Event('input', { bubbles: true }));
                        select.dispatchEvent(new Event('blur', { bubbles: true }));
                        
                        return select.value;
                    }
                    return null;
                """, other_relation_value)
                
                # Wait a moment for the selection to register
                time.sleep(2)
                
                # Verify selection using JavaScript to avoid stale element references
                try:
                    selected_value = driver.execute_script("""
                        var select = document.querySelector("select[data-pgr-id='ddlDriverRelationship']");
                        return select ? select.value : null;
                    """)
                    print(f"Selected 'Other relation' option (value: {other_relation_value})")
                    print(f"Verified selected value: {selected_value}")
                except Exception as e:
                    print(f"Could not verify selection (element may have been updated): {e}")
                    # Selection likely succeeded, continue anyway
                
                print(f"After relationship selection - Title: {driver.title}")
                print(f"After relationship selection - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver relationship dropdown")
                raise HTTPException(
                    status_code=404,
                    detail="Driver relationship dropdown not found"
                )
            except Exception as e:
                print(f"Error selecting driver relationship: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to select driver relationship: {str(e)}"
                )
            
            # -------------------------------------------------------------------------
            # STEP 20: Select "3 years or more" from driver years licensed range dropdown
            # -------------------------------------------------------------------------
            
            print("Looking for driver years licensed range dropdown...")
            
            try:
                # Find the dropdown by its data-pgr-id attribute
                years_licensed_dropdown = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlDriverYearsLicensedRange']"))
                )
                print("Found driver years licensed range dropdown")
                
                # Scroll to the dropdown with extra offset to avoid sticky headers
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", years_licensed_dropdown)
                time.sleep(1)
                
                # Use JavaScript click to open the dropdown (same pattern as requester dropdown)
                driver.execute_script("arguments[0].focus();", years_licensed_dropdown)
                driver.execute_script("arguments[0].click();", years_licensed_dropdown)
                time.sleep(1)
                
                # Select "3 years or more" option (value='3') using JavaScript
                # Use document.querySelector to avoid stale element references
                three_years_value = "3"
                driver.execute_script("""
                    var select = document.querySelector("select[data-pgr-id='ddlDriverYearsLicensedRange']");
                    if (select) {
                        select.value = arguments[0];
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                        select.dispatchEvent(new Event('input', { bubbles: true }));
                        select.dispatchEvent(new Event('blur', { bubbles: true }));
                    }
                """, three_years_value)
                
                print(f"Selected '3 years or more' option (value: {three_years_value})")
                
                # Wait a moment for the selection to register
                time.sleep(2)
                
                # Verify selection - re-find element to avoid stale reference
                try:
                    years_licensed_dropdown = driver.find_element(By.CSS_SELECTOR, "select[data-pgr-id='ddlDriverYearsLicensedRange']")
                    selected_value = years_licensed_dropdown.get_attribute('value')
                    print(f"Verified selected value: {selected_value}")
                except (StaleElementReferenceException, Exception) as e:
                    print(f"Could not verify selection (element may have been updated): {e}")
                    # Selection likely succeeded, continue anyway
                
                print(f"After years licensed selection - Title: {driver.title}")
                print(f"After years licensed selection - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver years licensed range dropdown")
                raise HTTPException(
                    status_code=404,
                    detail="Driver years licensed range dropdown not found"
                )
            except Exception as e:
                print(f"Error selecting driver years licensed range: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to select driver years licensed range: {str(e)}"
                )
            
            # -------------------------------------------------------------------------
            # STEP 21: Click "No" radio button for driver additional insured indicator
            # -------------------------------------------------------------------------
            
            print("Looking for driver additional insured indicator 'No' radio button...")
            
            try:
                # Find the "No" radio button by its data-pgr-id and value attributes
                no_radio = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-pgr-id='radDriverAdditionalInsuredIndicator150'][value='N']"))
                )
                print("Found 'No' radio button for additional insured indicator")
                
                # Scroll to the radio button
                driver.execute_script("arguments[0].scrollIntoView(true);", no_radio)
                time.sleep(1)
                
                # Click the "No" radio button
                driver.execute_script("arguments[0].click();", no_radio)
                print("Clicked 'No' for driver additional insured indicator")
                
                # Wait a moment for the selection to register
                time.sleep(2)
                
                print(f"After additional insured indicator selection - Title: {driver.title}")
                print(f"After additional insured indicator selection - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver additional insured indicator 'No' radio button")
                raise HTTPException(
                    status_code=404,
                    detail="Driver additional insured indicator 'No' radio button not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 22: Click the "Continue" button and wait for new page to load
            # -------------------------------------------------------------------------
            
            print("Looking for 'Continue' button...")
            
            try:
                # Find the Continue button by its data-pgr-id attribute
                continue_button = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
                )
                print("Found 'Continue' button")
                
                # Scroll to the button
                driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
                time.sleep(1)
                
                # Click the Continue button
                driver.execute_script("arguments[0].click();", continue_button)
                print("Clicked 'Continue' button")
                
                # Wait for the new page to load
                time.sleep(5)
                
                print(f"After Continue click - Title: {driver.title}")
                print(f"After Continue click - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find 'Continue' button")
                raise HTTPException(
                    status_code=404,
                    detail="Continue button not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 23: Click "No" radio button for driver violations
            # -------------------------------------------------------------------------
            
            print("Looking for driver violations 'No' radio button...")
            
            try:
                # Find the "No" label by its data-pgr-id, then find the associated input element
                no_label = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "pui-input-label[data-pgr-id='lblDriverHasViolationsUINo']"))
                )
                print("Found 'No' label for driver violations")
                
                # Find the associated input element (radio button) in the ancestor label
                no_input = extended_wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//pui-input-label[@data-pgr-id='lblDriverHasViolationsUINo']/ancestor::label//input"))
                )
                print("Found 'No' input element for driver violations")
                
                # Scroll to the input element
                driver.execute_script("arguments[0].scrollIntoView(true);", no_input)
                time.sleep(1)
                
                # Click the "No" input element
                driver.execute_script("arguments[0].click();", no_input)
                print("Clicked 'No' for driver violations")
                
                # Wait a moment for the selection to register
                time.sleep(2)
                
                print(f"After driver violations selection - Title: {driver.title}")
                print(f"After driver violations selection - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find driver violations 'No' radio button")
                raise HTTPException(
                    status_code=404,
                    detail="Driver violations 'No' radio button not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 24: Click checkbox to mark it
            # -------------------------------------------------------------------------
            
            print("Looking for checkbox to mark...")
            
            try:
                # Find the checkbox wrapper div, then find the associated input element
                # The checkbox wrapper has class "checkbox relative"
                checkbox_wrapper = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.checkbox.relative"))
                )
                print("Found checkbox wrapper")
                
                # Find the associated input element (checkbox) - it might be in a parent label or nearby
                # Try to find input[type='checkbox'] in the same label or nearby
                try:
                    checkbox_input = checkbox_wrapper.find_element(By.XPATH, "./ancestor::label//input[@type='checkbox']")
                except NoSuchElementException:
                    # Try alternative: find checkbox input in parent label
                    checkbox_input = extended_wait.until(
                        EC.element_to_be_clickable((By.XPATH, "//div[@class='checkbox relative']/ancestor::label//input[@type='checkbox']"))
                    )
                
                print("Found checkbox input element")
                
                # Scroll to the checkbox
                driver.execute_script("arguments[0].scrollIntoView(true);", checkbox_input)
                time.sleep(1)
                
                # Click the checkbox to mark it
                driver.execute_script("arguments[0].click();", checkbox_input)
                print("Clicked checkbox to mark it")
                
                # Wait a moment for the selection to register
                time.sleep(2)
                
                print(f"After checkbox click - Title: {driver.title}")
                print(f"After checkbox click - URL: {driver.current_url}")
                
            except (TimeoutException, NoSuchElementException) as e:
                print(f"Could not find checkbox: {str(e)}")
                # Try alternative approach - find first checkbox on the page after violations
                try:
                    print("Trying alternative approach to find checkbox...")
                    checkbox_input = extended_wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='checkbox']"))
                    )
                    driver.execute_script("arguments[0].scrollIntoView(true);", checkbox_input)
                    time.sleep(1)
                    driver.execute_script("arguments[0].click();", checkbox_input)
                    print("Clicked checkbox (alternative method)")
                    time.sleep(2)
                except Exception as e2:
                    raise HTTPException(
                        status_code=404,
                        detail="Checkbox not found"
                    )
            
            # -------------------------------------------------------------------------
            # STEP 25: Click the "Continue" button and wait for next page to load
            # -------------------------------------------------------------------------
            
            print("Looking for 'Continue' button...")
            
            try:
                # Find the Continue button by its data-pgr-id attribute
                continue_button = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
                )
                print("Found 'Continue' button")
                
                # Scroll to the button
                driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
                time.sleep(1)
                
                # Click the Continue button
                driver.execute_script("arguments[0].click();", continue_button)
                print("Clicked 'Continue' button")
                
                # Wait for the new page to load
                time.sleep(5)
                
                print(f"After Continue click - Title: {driver.title}")
                print(f"After Continue click - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find 'Continue' button")
                raise HTTPException(
                    status_code=404,
                    detail="Continue button not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 26: Scrape final page data (Add/Update driver, Premium details)
            # -------------------------------------------------------------------------
            
            print("Scraping final page data for driver action...")
            
            try:
                # Wait for the final review page to fully load
                time.sleep(3)
                
                # Scrape Driver action field (Add Driver or Update Driver)
                driver_action_text = ""
                try:
                    driver_action_element = extended_wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "#transaction-messaging ps-markdown"))
                    )
                    driver_action_text = driver_action_element.text.strip()
                    print(f"Driver action: {driver_action_text}")
                except TimeoutException:
                    print("Could not find driver action field")
                    driver_action_text = "Not found"
                
                # Scrape Total Premium Increase
                total_premium_increase = ""
                try:
                    # Find h4 element containing "Total premium increase:"
                    premium_increase_elements = driver.find_elements(By.CSS_SELECTOR, "h4.f5-e.fwi.ma0")
                    for element in premium_increase_elements:
                        if "Total premium increase:" in element.text:
                            # Extract just the amount (e.g., "$792.52")
                            total_premium_increase = element.text.replace("Total premium increase:", "").strip()
                            print(f"Total premium increase: {total_premium_increase}")
                            break
                    if not total_premium_increase:
                        total_premium_increase = "Not found"
                except Exception as e:
                    print(f"Could not find total premium increase: {str(e)}")
                    total_premium_increase = "Not found"
                
                # Scrape New Policy Premium
                new_policy_premium = ""
                try:
                    new_premium_element = extended_wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-pgr-id='txtNewPremium'] span.review-item-embed"))
                    )
                    new_policy_premium = new_premium_element.text.strip()
                    print(f"New policy premium: {new_policy_premium}")
                except TimeoutException:
                    print("Could not find new policy premium field")
                    new_policy_premium = "Not found"
                
                # Scrape Policy Start Date
                policy_start_date = ""
                try:
                    start_date_element = extended_wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-pgr-id='txtStartsOn'] span"))
                    )
                    policy_start_date = start_date_element.text.strip()
                    print(f"Policy starts on: {policy_start_date}")
                except TimeoutException:
                    print("Could not find policy start date field")
                    policy_start_date = "Not found"
                
                # Scrape New Premium Description
                new_premium_description = ""
                try:
                    premium_description_element = extended_wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "pui-p[data-pgr-id='msgInternalMessage0'] p"))
                    )
                    new_premium_description = premium_description_element.text.strip()
                    print(f"New premium description: {new_premium_description}")
                except TimeoutException:
                    print("Could not find new premium description field")
                    new_premium_description = "Not found"
                
                # Scrape Transaction Name (Add driver, Update driver, etc.)
                transaction_name = ""
                try:
                    transaction_name_element = extended_wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "pui-h4[data-pgr-id='ttlTransactionName'] h4"))
                    )
                    transaction_name = transaction_name_element.text.strip()
                    print(f"Transaction name: {transaction_name}")
                except TimeoutException:
                    print("Could not find transaction name field")
                    transaction_name = "Not found"
                
                # Scrape Definition List fields (Effective date, Requester, Agent name, Policy period)
                effective_date = ""
                requester = ""
                agent_name_scraped = ""
                policy_period = ""
                
                try:
                    # Find the definition list
                    definition_list = extended_wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "dl[pui-definition-list]"))
                    )
                    print("Found definition list")
                    
                    # Find all definition terms (dt) and definitions (dd)
                    terms = definition_list.find_elements(By.CSS_SELECTOR, "dt[pui-definition-term]")
                    definitions = definition_list.find_elements(By.CSS_SELECTOR, "dd[pui-definition-definition]")
                    
                    # Match terms with their definitions
                    for i, term in enumerate(terms):
                        if i < len(definitions):
                            term_text = term.text.strip()
                            definition_text = definitions[i].text.strip()
                            
                            if "Effective date:" in term_text:
                                effective_date = definition_text
                                print(f"Effective date: {effective_date}")
                            elif "Requester:" in term_text:
                                requester = definition_text
                                print(f"Requester: {requester}")
                            elif "Agent name:" in term_text:
                                agent_name_scraped = definition_text
                                print(f"Agent name: {agent_name_scraped}")
                            elif "Policy period:" in term_text:
                                policy_period = definition_text
                                print(f"Policy period: {policy_period}")
                                
                except TimeoutException:
                    print("Could not find definition list")
                    effective_date = "Not found"
                    requester = "Not found"
                    agent_name_scraped = "Not found"
                    policy_period = "Not found"
                except Exception as e:
                    print(f"Error scraping definition list: {str(e)}")
                    effective_date = "Not found"
                    requester = "Not found"
                    agent_name_scraped = "Not found"
                    policy_period = "Not found"
                
                print("=" * 60)
                print("✅ Step 26 completed successfully!")
                print(f"✅ Scraped all final page data for driver action")
                print("=" * 60)
                
            except Exception as e:
                print(f"Error scraping final page data: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Error scraping final page data: {str(e)}"
                )
            
            # -------------------------------------------------------------------------
            # STEP 27: Click "View upcoming payments" link and scrape payment schedule
            # -------------------------------------------------------------------------
            
            print("Looking for 'View upcoming payments' link...")
            
            try:
                # Find and click the "View upcoming payments" link
                view_payments_link = extended_wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'View upcoming payments')]"))
                )
                print("Found 'View upcoming payments' link")
                
                # Scroll to the link
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", view_payments_link)
                time.sleep(1)
                
                # Click the link using JavaScript
                driver.execute_script("arguments[0].click();", view_payments_link)
                print("Clicked 'View upcoming payments' link")
                
                # Wait for the popup/modal to appear
                time.sleep(2)
                
                # Wait for the payment schedule table to be visible
                payment_table = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table[data-pgr-id='tblPaymentSchedule']"))
                )
                print("Payment schedule table loaded")
                
                # Scrape the payment schedule table rows
                payment_schedule = []
                table_rows = driver.find_elements(By.CSS_SELECTOR, "table[data-pgr-id='tblPaymentSchedule'] tbody tr")
                
                print(f"Found {len(table_rows)} payment schedule rows")
                
                for index, row in enumerate(table_rows):
                    try:
                        # Extract all td elements from the row
                        cells = row.find_elements(By.TAG_NAME, "td")
                        
                        if len(cells) >= 4:
                            # Extract date (from the span inside first td)
                            date_element = cells[0].find_element(By.TAG_NAME, "span")
                            date = date_element.text.strip()
                            
                            # Extract current amount
                            current_amount = cells[1].text.strip()
                            
                            # Extract new amount
                            new_amount = cells[2].text.strip()
                            
                            # Extract difference
                            difference = cells[3].text.strip()
                            
                            payment_schedule.append({
                                "date": date,
                                "current_amount": current_amount,
                                "new_amount": new_amount,
                                "difference": difference
                            })
                            
                            print(f"Row {index + 1}: {date} | Current: {current_amount} | New: {new_amount} | Diff: {difference}")
                            
                    except Exception as e:
                        print(f"Error parsing row {index + 1}: {str(e)}")
                        continue
                
                # Scrape the installment fee note
                installment_fee_note = ""
                try:
                    fee_note_element = driver.find_element(By.CSS_SELECTOR, "pui-p[data-pgr-id='ttlServiceChargeDescription'] p")
                    installment_fee_note = fee_note_element.text.strip()
                    print(f"Installment fee note: {installment_fee_note}")
                except Exception as e:
                    print(f"Could not find installment fee note: {str(e)}")
                    installment_fee_note = "Not found"
                
                # Close the payment schedule popup
                try:
                    close_button = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Close Modal']")
                    print("Found close button for payment schedule popup")
                    
                    # Click the close button using JavaScript
                    driver.execute_script("arguments[0].click();", close_button)
                    print("Clicked close button - popup closed")
                    
                    # Wait for popup to close
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"Could not find or click close button: {str(e)}")
                
                print("=" * 60)
                print("✅ Step 27 completed successfully!")
                print(f"✅ Scraped {len(payment_schedule)} payment schedule entries")
                print("=" * 60)
                
            except TimeoutException:
                print("Could not find 'View upcoming payments' link or payment schedule table")
                raise HTTPException(
                    status_code=404,
                    detail="View upcoming payments link or payment schedule table not found"
                )
            except Exception as e:
                print(f"Error scraping payment schedule: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Error scraping payment schedule: {str(e)}"
                )
            
            # -------------------------------------------------------------------------
            # STEP 28: Click "effect on rate for the entire policy period" link and scrape coverage comparison data
            # -------------------------------------------------------------------------
            
            print("Looking for 'effect on rate for the entire policy period' link...")
            
            try:
                # Find and click the effect on rate link
                effect_on_rate_link = extended_wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'effect on rate for the entire policy period')]"))
                )
                print("Found 'effect on rate for the entire policy period' link")
                
                # Scroll to the link
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", effect_on_rate_link)
                time.sleep(1)
                
                # Click the link using JavaScript
                driver.execute_script("arguments[0].click();", effect_on_rate_link)
                print("Clicked 'effect on rate for the entire policy period' link")
                
                # Wait for the modal to appear
                time.sleep(2)
                
                # Wait for the modal content to be visible
                extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "pui-modal-body"))
                )
                print("Effect on rate modal loaded")
                
                # Initialize data structure for storing all scraped data
                effect_on_rate_data = {
                    "vehicle_summary": [],
                    "total_policy_rate": {},
                    "vehicle_details": []
                }
                
                # Scrape Vehicle Summary section (top section with vehicle totals)
                print("Scraping vehicle summary section...")
                try:
                    vehicle_summary_elements = driver.find_elements(By.XPATH, "//pui-h3[contains(text(), 'Vehicle')]/following-sibling::div//pui-p[@fw='7']")
                    
                    for vehicle_elem in vehicle_summary_elements:
                        vehicle_name = vehicle_elem.text.strip()
                        
                        # Find the corresponding table for this vehicle
                        parent_div = vehicle_elem.find_element(By.XPATH, "./ancestor::div[contains(@class, 'ng-star-inserted')]")
                        table_rows = parent_div.find_elements(By.CSS_SELECTOR, "table tbody tr")
                        
                        if len(table_rows) > 0:
                            cells = table_rows[0].find_elements(By.TAG_NAME, "td")
                            if len(cells) >= 2:
                                current_value = cells[0].text.strip()
                                new_value = cells[1].text.strip()
                                
                                effect_on_rate_data["vehicle_summary"].append({
                                    "vehicle_name": vehicle_name,
                                    "current_rate": current_value,
                                    "new_rate": new_value
                                })
                                print(f"Vehicle: {vehicle_name} | Current: {current_value} | New: {new_value}")
                                
                except Exception as e:
                    print(f"Error scraping vehicle summary: {str(e)}")
                
                # Scrape Total Policy Rate section
                print("Scraping total policy rate...")
                try:
                    total_policy_elements = driver.find_elements(By.XPATH, "//pui-p[contains(text(), 'Total Policy Rate')]")
                    
                    if total_policy_elements:
                        parent_div = total_policy_elements[0].find_element(By.XPATH, "./ancestor::div[contains(@class, 'ng-star-inserted')]")
                        table_rows = parent_div.find_elements(By.CSS_SELECTOR, "table tbody tr")
                        
                        if len(table_rows) > 0:
                            cells = table_rows[0].find_elements(By.TAG_NAME, "td")
                            if len(cells) >= 2:
                                current_value = cells[0].text.strip()
                                new_value = cells[1].text.strip()
                                
                                effect_on_rate_data["total_policy_rate"] = {
                                    "current_rate": current_value,
                                    "new_rate": new_value
                                }
                                print(f"Total Policy Rate | Current: {current_value} | New: {new_value}")
                                
                except Exception as e:
                    print(f"Error scraping total policy rate: {str(e)}")
                
                # Scrape detailed vehicle breakdowns (after the hr separator)
                print("Scraping detailed vehicle breakdowns...")
                try:
                    # Find all vehicle headers (h4 elements with vehicle names)
                    vehicle_headers = driver.find_elements(By.XPATH, "//pui-hr/following-sibling::pui-h4[@class='db f4 fw6 lh-title mv2 outline-0 pgr-dark-blue ng-star-inserted']")
                    
                    for vehicle_header in vehicle_headers:
                        vehicle_name = vehicle_header.text.strip()
                        print(f"Processing detailed breakdown for: {vehicle_name}")
                        
                        vehicle_data = {
                            "vehicle_name": vehicle_name,
                            "coverages": []
                        }
                        
                        # Find all coverage sections for this vehicle
                        # Look for pui-p elements with fw="7" that follow this h4
                        current_element = vehicle_header
                        
                        while True:
                            try:
                                # Find the next sibling div
                                next_sibling = current_element.find_element(By.XPATH, "./following-sibling::div[1]")
                                
                                # Check if this is another vehicle header (stop if so)
                                try:
                                    next_header = next_sibling.find_element(By.XPATH, "./preceding-sibling::pui-h4[1]")
                                    if next_header != vehicle_header:
                                        break
                                except:
                                    pass
                                
                                # Try to find coverage name in this div
                                try:
                                    coverage_name_elem = next_sibling.find_element(By.CSS_SELECTOR, "pui-p[fw='7'] p span")
                                    coverage_name = coverage_name_elem.text.strip()
                                    
                                    # Find the table in this div
                                    table = next_sibling.find_element(By.CSS_SELECTOR, "table")
                                    table_rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
                                    
                                    # Extract current and new values
                                    current_coverage = ""
                                    current_value = ""
                                    new_coverage = ""
                                    new_value = ""
                                    
                                    for row_index, row in enumerate(table_rows):
                                        cells = row.find_elements(By.TAG_NAME, "td")
                                        
                                        if row_index == 0 and len(cells) >= 2:
                                            # First row contains coverage details
                                            current_coverage = cells[0].text.strip()
                                            new_coverage = cells[1].text.strip()
                                        elif row_index == 1 and len(cells) >= 2:
                                            # Second row contains values
                                            current_value = cells[0].text.strip()
                                            new_value = cells[1].text.strip()
                                    
                                    vehicle_data["coverages"].append({
                                        "coverage_name": coverage_name,
                                        "current_coverage": current_coverage,
                                        "current_value": current_value,
                                        "new_coverage": new_coverage,
                                        "new_value": new_value
                                    })
                                    
                                    print(f"  - {coverage_name}: Current ${current_value} -> New ${new_value}")
                                    
                                except:
                                    pass
                                
                                current_element = next_sibling
                                
                            except:
                                break
                        
                        effect_on_rate_data["vehicle_details"].append(vehicle_data)
                        
                except Exception as e:
                    print(f"Error scraping vehicle details: {str(e)}")
                
                # Close the effect on rate modal
                try:
                    close_button = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Close Modal']")
                    print("Found close button for effect on rate modal")
                    
                    # Scroll to button
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", close_button)
                    time.sleep(1)
                    
                    # Click the close button using JavaScript
                    driver.execute_script("arguments[0].click();", close_button)
                    print("Clicked close button - effect on rate modal closed")
                    
                    # Wait for modal to close
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"Could not find or click close button: {str(e)}")
                
                print("=" * 60)
                print("✅ Step 28 completed successfully!")
                print(f"✅ Scraped effect on rate data for {len(effect_on_rate_data['vehicle_details'])} vehicles")
                print("=" * 60)
                
            except TimeoutException:
                print("Could not find 'effect on rate for the entire policy period' link or modal")
                raise HTTPException(
                    status_code=404,
                    detail="Effect on rate link or modal not found"
                )
            except Exception as e:
                print(f"Error scraping effect on rate data: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Error scraping effect on rate data: {str(e)}"
                )
            
            # -------------------------------------------------------------------------
            # STEP 29: Click "Save this update for later" checkbox
            # -------------------------------------------------------------------------
            
            print("Looking for 'Save this update for later' checkbox...")
            
            try:
                # Find the label by its data-pgr-id, then find the associated input element
                save_label = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "pui-input-label[data-pgr-id='lblQuoteBeforeContinueOptionSave this update for later']"))
                )
                print("Found 'Save this update for later' label")
                
                # Find the associated input element (checkbox) in the ancestor label
                save_checkbox = extended_wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//pui-input-label[@data-pgr-id='lblQuoteBeforeContinueOptionSave this update for later']/ancestor::label//input"))
                )
                print("Found 'Save this update for later' checkbox")
                
                # Scroll to the checkbox
                driver.execute_script("arguments[0].scrollIntoView(true);", save_checkbox)
                time.sleep(1)
                
                # Click the checkbox to mark it
                driver.execute_script("arguments[0].click();", save_checkbox)
                print("Clicked 'Save this update for later' checkbox")
                
                # Wait a moment for the selection to register
                time.sleep(2)
                
                print(f"After checkbox click - Title: {driver.title}")
                print(f"After checkbox click - URL: {driver.current_url}")
                
            except TimeoutException:
                print("Could not find 'Save this update for later' checkbox")
                raise HTTPException(
                    status_code=404,
                    detail="Save this update for later checkbox not found"
                )
            
            # -------------------------------------------------------------------------
            # STEP 30: Click the final "Continue" button and wait for next page to load, then end bot
            # -------------------------------------------------------------------------
            
            print("Looking for final 'Continue' button...")
            
            try:
                # Find the Continue button by its data-pgr-id attribute
                continue_button = extended_wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
                )
                print("Found final 'Continue' button")
                
                # Scroll to the button
                driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
                time.sleep(1)
                
                # Click the Continue button
                driver.execute_script("arguments[0].click();", continue_button)
                print("Clicked final 'Continue' button")
                
                # Wait for the new page to load
                time.sleep(5)
                
                print(f"After final Continue click - Title: {driver.title}")
                print(f"After final Continue click - URL: {driver.current_url}")
                
                print("=" * 60)
                log_thread(thread_id, "✅ Step 30 completed successfully!")
                log_thread(thread_id, "✅ Final page loaded - Bot process completed")
                log_thread(thread_id, "=" * 60)
                
                # Update thread status
                with browser_threads_lock:
                    if thread_id in browser_threads:
                        browser_threads[thread_id]["status"] = "completed"
                
            except TimeoutException:
                log_thread(thread_id, "Could not find final 'Continue' button")
                raise HTTPException(
                    status_code=404,
                    detail="Final Continue button not found"
                )
            
            # Prepare response data before cleanup
            response_data = {
                "success": True,
                "message": "Driver action process completed successfully",
                "policy_number": request.policy_no,
                "transaction_name": transaction_name,
                "driver_action": driver_action_text,
                "transaction_details": {
                    "effective_date": effective_date,
                    "requester": requester,
                    "agent_name": agent_name_scraped,
                    "policy_period": policy_period
                },
                "premium_details": {
                    "total_premium_increase": total_premium_increase,
                    "new_policy_premium": new_policy_premium,
                    "policy_start_date": policy_start_date,
                    "new_premium_description": new_premium_description
                },
                "payment_schedule": payment_schedule,
                "installment_fee_note": installment_fee_note,
                "effect_on_rate": effect_on_rate_data
            }
            
            log_thread(thread_id, "📦 Preparing to send response to client...")
            
            # Close browser before returning response to avoid hanging
            if driver:
                try:
                    log_thread(thread_id, "🔧 Closing browser...")
                    driver.quit()
                    log_thread(thread_id, "✅ Browser closed successfully")
                except Exception as e:
                    log_thread(thread_id, f"⚠️  Warning: Error closing browser: {str(e)}")
            
            log_thread(thread_id, "=" * 60)
            log_thread(thread_id, "✅ Sending response to client")
            log_thread(thread_id, "=" * 60)
            
            # Return scraped data
            return response_data
        elif "replace" in action_type_lower:
            # Only perform vehicle selection for "replace vehical" action
            print("Looking for vehicle list...")
            print(f"Vehicle name to match: {request.vehicle_name_to_replace}")
            
            try:
                # Wait for vehicle radio buttons to be present
                time.sleep(3)
                
                # Find all radio buttons for vehicles
                vehicle_radios = driver.find_elements(By.CSS_SELECTOR, "input[data-pgr-id='radTranVehicleIndex0']")
                print(f"Found {len(vehicle_radios)} vehicle options")
                
                if not vehicle_radios:
                    raise Exception("No vehicle options found")
                
                # Search for the matching vehicle
                best_match = None
                best_match_score = 0
                vehicle_name_upper = request.vehicle_name_to_replace.upper()
                
                for radio in vehicle_radios:
                    try:
                        # Get the parent label to find the vehicle name
                        parent_label = radio.find_element(By.XPATH, "./ancestor::label")
                        
                        # Find the vehicle name in the ps-markdown element
                        vehicle_name_element = parent_label.find_element(By.CSS_SELECTOR, "pui-input-label.ng-star-inserted ps-markdown")
                        vehicle_name = vehicle_name_element.text.strip().upper()
                        
                        print(f"Checking vehicle: {vehicle_name}")
                        
                        # Calculate match score (count matching words/characters)
                        # Check if the payload vehicle name is contained in the full vehicle name
                        if vehicle_name_upper in vehicle_name:
                            match_score = len(vehicle_name_upper)
                        elif vehicle_name in vehicle_name_upper:
                            match_score = len(vehicle_name)
                        else:
                            # Count matching words
                            payload_words = vehicle_name_upper.split()
                            match_score = sum(1 for word in payload_words if word in vehicle_name)
                        
                        print(f"  Match score: {match_score}")
                        
                        if match_score > best_match_score:
                            best_match_score = match_score
                            best_match = {
                                "radio": radio,
                                "name": vehicle_name
                            }
                            
                    except Exception as e:
                        print(f"  Error processing vehicle option: {e}")
                        continue
                
                if not best_match or best_match_score == 0:
                    raise Exception(f"No matching vehicle found for: {request.vehicle_name_to_replace}")
                
                print(f"Best match found: {best_match['name']} (score: {best_match_score})")
                
                # Scroll to the radio button
                driver.execute_script("arguments[0].scrollIntoView(true);", best_match['radio'])
                time.sleep(1)
                
                # Click the radio button
                driver.execute_script("arguments[0].click();", best_match['radio'])
                print(f"Selected vehicle: {best_match['name']}")
                
                # Wait for selection to register
                time.sleep(2)
                
                print(f"After vehicle selection - Title: {driver.title}")
                print(f"After vehicle selection - URL: {driver.current_url}")
                
            except Exception as e:
                print(f"Error finding/selecting vehicle: {str(e)}")
                raise HTTPException(
                    status_code=404,
                    detail=f"Could not find matching vehicle for: {request.vehicle_name_to_replace}"
                )
        else:
            print("Skipping vehicle selection (add vehicle action - no existing vehicle to select)")
        
        # -------------------------------------------------------------------------
        # STEP 15: Click the "Continue" button after vehicle selection
        # -------------------------------------------------------------------------
        
        print("Looking for 'Continue' button after vehicle selection...")
        
        try:
            # Find the Continue button by its data-pgr-id attribute
            continue_button = extended_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
            )
            print("Found 'Continue' button")
            
            # Scroll to the button
            driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
            time.sleep(1)
            
            # Click the Continue button
            driver.execute_script("arguments[0].click();", continue_button)
            print("Clicked 'Continue' button")
            
            # Wait for the new page to load
            time.sleep(5)
            
            print(f"After Continue click - Title: {driver.title}")
            print(f"After Continue click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find 'Continue' button after vehicle selection")
            raise HTTPException(
                status_code=404,
                detail="Continue button not found after vehicle selection"
            )
        
        # -------------------------------------------------------------------------
        # STEP 16: Select the vehicle year from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for vehicle year dropdown...")
        print(f"Year to select: {request.vehical_year}")
        
        try:
            # Find the year dropdown by its data-pgr-id attribute
            year_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlVehicleModelYearTemp']"))
            )
            print("Found vehicle year dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", year_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", year_dropdown)
            driver.execute_script("arguments[0].click();", year_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, year_dropdown, request.vehical_year)
            
            print(f"Selected year: {request.vehical_year}")
            
            # Verify selection
            selected_value = year_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After year selection - Title: {driver.title}")
            print(f"After year selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find vehicle year dropdown")
            raise HTTPException(
                status_code=404,
                detail="Vehicle year dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 17: Select Yes/No for conversion van/pickup/SUV question
        # -------------------------------------------------------------------------
        
        print("Looking for conversion van/pickup/SUV radio buttons...")
        
        # Normalize the input value (accept yes/Yes/Y or no/No/N)
        answer_upper = request.vehical_is_suv_van_pickup.upper().strip()
        if answer_upper in ['YES', 'Y']:
            selected_value = 'Y'
            selected_text = 'Yes'
        elif answer_upper in ['NO', 'N']:
            selected_value = 'N'
            selected_text = 'No'
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid value for vehical_is_suv_van_pickup: {request.vehical_is_suv_van_pickup}. Must be 'yes' or 'no'"
            )
        
        print(f"Selecting: {selected_text} (value: {selected_value})")
        
        try:
            # Find the radio button by value attribute
            radio_button = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f"input[name='VehicleIsConversionVan10'][value='{selected_value}']"))
            )
            print(f"Found {selected_text} radio button")
            
            # Scroll to the radio button
            driver.execute_script("arguments[0].scrollIntoView(true);", radio_button)
            time.sleep(1)
            
            # Click the radio button
            driver.execute_script("arguments[0].click();", radio_button)
            print(f"Selected: {selected_text}")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After conversion van selection - Title: {driver.title}")
            print(f"After conversion van selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print(f"Could not find {selected_text} radio button")
            raise HTTPException(
                status_code=404,
                detail=f"Conversion van/pickup/SUV radio button ({selected_text}) not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 18: Select Yes/No for kit car/buggy/classic question
        # -------------------------------------------------------------------------
        
        print("Looking for kit car/buggy/classic radio buttons...")
        
        # Normalize the input value (accept yes/Yes/Y or no/No/N)
        answer_upper2 = request.vehical_is_kitcar_buggy_classic.upper().strip()
        if answer_upper2 in ['YES', 'Y']:
            selected_value2 = 'Y'
            selected_text2 = 'Yes'
        elif answer_upper2 in ['NO', 'N']:
            selected_value2 = 'N'
            selected_text2 = 'No'
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid value for vehical_is_kitcar_buggy_classic: {request.vehical_is_kitcar_buggy_classic}. Must be 'yes' or 'no'"
            )
        
        print(f"Selecting: {selected_text2} (value: {selected_value2})")
        
        try:
            # Find the radio button by value attribute
            radio_button2 = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f"input[name='VehicleIsSpecialType20'][value='{selected_value2}']"))
            )
            print(f"Found {selected_text2} radio button")
            
            # Scroll to the radio button
            driver.execute_script("arguments[0].scrollIntoView(true);", radio_button2)
            time.sleep(1)
            
            # Click the radio button
            driver.execute_script("arguments[0].click();", radio_button2)
            print(f"Selected: {selected_text2}")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After kit car/buggy/classic selection - Title: {driver.title}")
            print(f"After kit car/buggy/classic selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print(f"Could not find {selected_text2} radio button")
            raise HTTPException(
                status_code=404,
                detail=f"Kit car/buggy/classic radio button ({selected_text2}) not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 19: Select "No" for VIN knowledge question (always No)
        # -------------------------------------------------------------------------
        
        print("Looking for VIN knowledge radio buttons...")
        print("Selecting: No (always)")
        
        try:
            # Find the "No" radio button by value attribute (always select No)
            radio_button3 = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='CurrentVehicleVinKnownInd30'][value='N']"))
            )
            print("Found No radio button for VIN knowledge")
            
            # Scroll to the radio button
            driver.execute_script("arguments[0].scrollIntoView(true);", radio_button3)
            time.sleep(1)
            
            # Click the radio button
            driver.execute_script("arguments[0].click();", radio_button3)
            print("Selected: No (VIN knowledge)")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After VIN knowledge selection - Title: {driver.title}")
            print(f"After VIN knowledge selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find No radio button for VIN knowledge")
            raise HTTPException(
                status_code=404,
                detail="VIN knowledge radio button (No) not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 20: Select vehicle make from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for vehicle make dropdown...")
        print(f"Make to select: {request.make}")
        
        try:
            # Find the make dropdown by its data-pgr-id attribute
            make_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlVehicleMake']"))
            )
            print("Found vehicle make dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", make_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", make_dropdown)
            driver.execute_script("arguments[0].click();", make_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            make_value = request.make.upper()  # Convert to uppercase to match options
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, make_dropdown, make_value)
            
            print(f"Selected make: {make_value}")
            
            # Verify selection
            selected_value = make_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After make selection - Title: {driver.title}")
            print(f"After make selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find vehicle make dropdown")
            raise HTTPException(
                status_code=404,
                detail="Vehicle make dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 21: Select vehicle model from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for vehicle model dropdown...")
        print(f"Model to select: {request.model}")
        
        try:
            # Find the model dropdown by its data-pgr-id attribute
            model_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlVehicleModel']"))
            )
            print("Found vehicle model dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", model_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", model_dropdown)
            driver.execute_script("arguments[0].click();", model_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            model_value = request.model.upper()  # Convert to uppercase to match options
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, model_dropdown, model_value)
            
            print(f"Selected model: {model_value}")
            
            # Verify selection
            selected_value = model_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After model selection - Title: {driver.title}")
            print(f"After model selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find vehicle model dropdown")
            raise HTTPException(
                status_code=404,
                detail="Vehicle model dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 22: Select body style (if field appears) - Optional step
        # Handles 3 cases: auto-selected, radio buttons, or dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for body style field...")
        
        body_style_selected = None
        
        # Case 1: Try to find body style as a dropdown
        try:
            print("Checking for body style dropdown...")
            body_style_dropdown = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlVeh_Sym_Sel']"))
            )
            print("Found body style dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", body_style_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", body_style_dropdown)
            driver.execute_script("arguments[0].click();", body_style_dropdown)
            time.sleep(1)
            
            # Select the first non-empty option (index 1, since index 0 is usually empty)
            select = Select(body_style_dropdown)
            select.select_by_index(1)
            
            # Get the selected option text
            selected_option = select.first_selected_option
            body_style_selected = selected_option.text.strip()
            print(f"Selected first body style from dropdown: {body_style_selected}")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After body style dropdown selection - Title: {driver.title}")
            print(f"After body style dropdown selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Body style dropdown not found, checking for radio buttons...")
            
            # Case 2: Try to find body style as radio buttons
            try:
                body_style_radios = WebDriverWait(driver, 3).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, "input[data-pgr-id='radVeh_Sym_Sel60']"))
                )
                
                if body_style_radios and len(body_style_radios) > 0:
                    print(f"Found {len(body_style_radios)} body style radio button options")
                    
                    # Select the first radio button
                    first_radio = body_style_radios[0]
                    
                    # Get the label text for logging
                    try:
                        parent_label = first_radio.find_element(By.XPATH, "./ancestor::label")
                        label_element = parent_label.find_element(By.CSS_SELECTOR, "pui-input-label ps-markdown")
                        body_style_text = label_element.text.strip()
                        print(f"Selecting first body style radio option: {body_style_text}")
                    except:
                        body_style_text = first_radio.get_attribute('value')
                        print(f"Selecting first body style radio option with value: {body_style_text}")
                    
                    # Scroll to the radio button
                    driver.execute_script("arguments[0].scrollIntoView(true);", first_radio)
                    time.sleep(1)
                    
                    # Click the radio button
                    driver.execute_script("arguments[0].click();", first_radio)
                    print(f"Selected body style: {body_style_text}")
                    
                    body_style_selected = body_style_text
                    
                    # Wait for selection to register
                    time.sleep(2)
                    
                    print(f"After body style radio selection - Title: {driver.title}")
                    print(f"After body style radio selection - URL: {driver.current_url}")
                else:
                    print("No body style radio options found (field may be pre-filled)")
                    body_style_selected = "Pre-filled or not applicable"
                    
            except TimeoutException:
                # Case 3: Field not found - it's pre-filled or not required
                print("⚠️ Body style field not found (radio or dropdown) - field may be pre-filled or not required")
                print("✅ Continuing without body style selection...")
                body_style_selected = "Pre-filled or not applicable"
            except Exception as e:
                print(f"⚠️ Error finding body style field: {e}")
                print("✅ Continuing without body style selection...")
                body_style_selected = "Pre-filled or not applicable"
        except Exception as e:
            print(f"⚠️ Error with body style dropdown: {e}")
            print("✅ Continuing without body style selection...")
            body_style_selected = "Pre-filled or not applicable"
        
        # -------------------------------------------------------------------------
        # STEP 23: Click the "Continue" button after body style selection
        # -------------------------------------------------------------------------
        
        print("Looking for 'Continue' button after body style...")
        
        try:
            # Find the Continue button by its data-pgr-id attribute
            continue_button = extended_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
            )
            print("Found 'Continue' button")
            
            # Scroll to the button
            driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
            time.sleep(1)
            
            # Click the Continue button
            driver.execute_script("arguments[0].click();", continue_button)
            print("Clicked 'Continue' button")
            
            # Wait for the new page to load
            time.sleep(5)
            
            print(f"After Continue click - Title: {driver.title}")
            print(f"After Continue click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find 'Continue' button after body style")
            raise HTTPException(
                status_code=404,
                detail="Continue button not found after body style selection"
            )
        
        # -------------------------------------------------------------------------
        # STEP 24: Select "No" for anti-theft device question (always No)
        # -------------------------------------------------------------------------
        
        print("Looking for anti-theft device radio button...")
        
        try:
            # Find the "No" radio button for anti-theft device
            # Using XPath to find the label with data-pgr-id containing "lblVehicleAntitheftDeviceCodeNo"
            antitheft_radio = extended_wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//pui-input-label[@data-pgr-id='lblVehicleAntitheftDeviceCodeNo']/ancestor::label/input[@type='radio']")
                )
            )
            print("Found 'No' radio button for anti-theft device")
            
            # Scroll to the radio button
            driver.execute_script("arguments[0].scrollIntoView(true);", antitheft_radio)
            time.sleep(1)
            
            # Click the radio button
            driver.execute_script("arguments[0].click();", antitheft_radio)
            print("Selected: No (anti-theft device)")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After anti-theft selection - Title: {driver.title}")
            print(f"After anti-theft selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find anti-theft device radio button")
            raise HTTPException(
                status_code=404,
                detail="Anti-theft device radio button not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 25: Click the "Continue" button after anti-theft selection
        # -------------------------------------------------------------------------
        
        print("Looking for 'Continue' button after anti-theft selection...")
        
        try:
            # Find the Continue button by its data-pgr-id attribute
            continue_button = extended_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
            )
            print("Found 'Continue' button")
            
            # Scroll to the button
            driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
            time.sleep(1)
            
            # Click the Continue button
            driver.execute_script("arguments[0].click();", continue_button)
            print("Clicked 'Continue' button")
            
            # Wait for the new page to load
            time.sleep(5)
            
            print(f"After Continue click - Title: {driver.title}")
            print(f"After Continue click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find 'Continue' button after anti-theft selection")
            raise HTTPException(
                status_code=404,
                detail="Continue button not found after anti-theft selection"
            )
        
        # -------------------------------------------------------------------------
        # STEP 26: Select vehicle use from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for vehicle use dropdown...")
        print(f"Vehicle use to select: {request.vehicle_use}")
        
        # Map vehicle use text to dropdown values
        vehicle_use_map = {
            "COMMUTE": "4",
            "PLEASURE/PERSONAL": "1",
            "PLEASURE": "1",
            "PERSONAL": "1",
            "BUSINESS": "2",
            "FARM": "3"
        }
        
        vehicle_use_upper = request.vehicle_use.upper().strip()
        vehicle_use_value = vehicle_use_map.get(vehicle_use_upper)
        
        if not vehicle_use_value:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid vehicle_use: {request.vehicle_use}. Must be one of: Commute, Pleasure/Personal, Business, Farm"
            )
        
        try:
            # Find the vehicle use dropdown by its data-pgr-id attribute
            vehicle_use_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlVehicleUse']"))
            )
            print("Found vehicle use dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", vehicle_use_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", vehicle_use_dropdown)
            driver.execute_script("arguments[0].click();", vehicle_use_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, vehicle_use_dropdown, vehicle_use_value)
            
            print(f"Selected vehicle use: {request.vehicle_use} (value: {vehicle_use_value})")
            
            # Verify selection
            selected_value = vehicle_use_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After vehicle use selection - Title: {driver.title}")
            print(f"After vehicle use selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find vehicle use dropdown")
            raise HTTPException(
                status_code=404,
                detail="Vehicle use dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 27: Select Yes/No for ridesharing question
        # -------------------------------------------------------------------------
        
        print("Looking for ridesharing radio buttons...")
        
        # Normalize the input value (accept yes/Yes/Y or no/No/N)
        ridesharing_upper = request.vehicle_use_ridesharing.upper().strip()
        if ridesharing_upper in ['YES', 'Y']:
            ridesharing_value = 'Y'
            ridesharing_text = 'Yes'
        elif ridesharing_upper in ['NO', 'N']:
            ridesharing_value = 'N'
            ridesharing_text = 'No'
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid value for vehicle_use_ridesharing: {request.vehicle_use_ridesharing}. Must be 'yes' or 'no'"
            )
        
        print(f"Selecting: {ridesharing_text} (value: {ridesharing_value})")
        
        try:
            # Find the radio button by value attribute
            ridesharing_radio = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f"input[name='VehicleTransportationNetworkCompanyCode10'][value='{ridesharing_value}']"))
            )
            print(f"Found {ridesharing_text} radio button for ridesharing")
            
            # Scroll to the radio button
            driver.execute_script("arguments[0].scrollIntoView(true);", ridesharing_radio)
            time.sleep(1)
            
            # Click the radio button
            driver.execute_script("arguments[0].click();", ridesharing_radio)
            print(f"Selected: {ridesharing_text} for ridesharing")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After ridesharing selection - Title: {driver.title}")
            print(f"After ridesharing selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print(f"Could not find {ridesharing_text} radio button for ridesharing")
            raise HTTPException(
                status_code=404,
                detail=f"Ridesharing radio button ({ridesharing_text}) not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 28: Enter one-way commute miles
        # -------------------------------------------------------------------------
        
        print("Looking for one-way commute miles input field...")
        print(f"Miles to enter: {request.one_way_commute_miles}")
        
        try:
            # Find the commute miles input field by its data-pgr-id attribute
            commute_miles_field = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-pgr-id='txtVehicleOneWayCommuteMiles']"))
            )
            print("Found one-way commute miles input field")
            
            # Scroll to the input field
            driver.execute_script("arguments[0].scrollIntoView(true);", commute_miles_field)
            time.sleep(1)
            
            # Clear the field first
            commute_miles_field.clear()
            time.sleep(0.5)
            
            # Enter the commute miles
            commute_miles_field.send_keys(request.one_way_commute_miles)
            print(f"Entered one-way commute miles: {request.one_way_commute_miles}")
            
            # Trigger change events to ensure the field registers the input
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", commute_miles_field)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", commute_miles_field)
            
            # Wait a moment for the field to register
            time.sleep(2)
            
            print(f"After commute miles entry - Title: {driver.title}")
            print(f"After commute miles entry - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find one-way commute miles input field")
            raise HTTPException(
                status_code=404,
                detail="One-way commute miles input field not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 29: Select "Mailing Address" for primary location (always)
        # -------------------------------------------------------------------------
        
        print("Looking for primary location (Mailing Address) radio button...")
        
        try:
            # Find the "Mailing Address" radio button
            # Using XPath to find the ps-markdown with "Mailing Address" text, then get the associated input
            mailing_address_radio = extended_wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//ps-markdown[contains(text(), 'Mailing Address')]/ancestor::label/input[@type='radio']")
                )
            )
            print("Found 'Mailing Address' radio button")
            
            # Scroll to the radio button
            driver.execute_script("arguments[0].scrollIntoView(true);", mailing_address_radio)
            time.sleep(1)
            
            # Click the radio button
            driver.execute_script("arguments[0].click();", mailing_address_radio)
            print("Selected: Mailing Address as primary location")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After primary location selection - Title: {driver.title}")
            print(f"After primary location selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find Mailing Address radio button")
            raise HTTPException(
                status_code=404,
                detail="Mailing Address radio button not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 30: Select vehicle ownership from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for vehicle ownership dropdown...")
        print(f"Vehicle ownership to select: {request.vehicle_ownership}")
        
        # Map vehicle ownership text to dropdown values
        ownership_map = {
            "LEASE": "1",
            "OWN AND MAKE PAYMENTS": "2",
            "OWN": "2",  # Shorthand for "Own and make payments"
            "OWN AND DO NOT MAKE PAYMENTS": "3",
            "OWN NO PAYMENTS": "3"  # Shorthand
        }
        
        ownership_upper = request.vehicle_ownership.upper().strip()
        ownership_value = ownership_map.get(ownership_upper)
        
        if not ownership_value:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid vehicle_ownership: {request.vehicle_ownership}. Must be one of: Lease, Own and make payments, Own and do not make payments"
            )
        
        try:
            # Retry logic for stale element references
            max_retries = 3
            retry_count = 0
            
            while retry_count < max_retries:
                try:
                    # Find the ownership dropdown by its data-pgr-id attribute
                    ownership_dropdown = extended_wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlVehicleFinancialOwnership']"))
                    )
                    print("Found vehicle ownership dropdown")
                    
                    # Scroll to the dropdown with extra offset to avoid sticky headers
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", ownership_dropdown)
                    time.sleep(1)
                    
                    # Use JavaScript click to avoid interception by sticky headers
                    driver.execute_script("arguments[0].focus();", ownership_dropdown)
                    driver.execute_script("arguments[0].click();", ownership_dropdown)
                    time.sleep(1)
                    
                    # Re-find element before setting value to avoid stale reference
                    ownership_dropdown = driver.find_element(By.CSS_SELECTOR, "select[data-pgr-id='ddlVehicleFinancialOwnership']")
                    
                    # Use JavaScript to set the value and trigger change events
                    driver.execute_script("""
                        var select = arguments[0];
                        select.value = arguments[1];
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                        select.dispatchEvent(new Event('input', { bubbles: true }));
                        select.dispatchEvent(new Event('blur', { bubbles: true }));
                    """, ownership_dropdown, ownership_value)
                    
                    print(f"Selected vehicle ownership: {request.vehicle_ownership} (value: {ownership_value})")
                    
                    # Re-find element before verification to avoid stale reference
                    ownership_dropdown = driver.find_element(By.CSS_SELECTOR, "select[data-pgr-id='ddlVehicleFinancialOwnership']")
                    
                    # Verify selection
                    selected_value = ownership_dropdown.get_attribute('value')
                    print(f"Verified selected value: {selected_value}")
                    
                    # Wait a moment for the selection to register
                    time.sleep(2)
                    
                    print(f"After ownership selection - Title: {driver.title}")
                    print(f"After ownership selection - URL: {driver.current_url}")
                    
                    # Success - break out of retry loop
                    break
                    
                except StaleElementReferenceException as e:
                    retry_count += 1
                    print(f"Stale element reference (attempt {retry_count}/{max_retries}). Retrying...")
                    if retry_count >= max_retries:
                        print("Max retries reached for vehicle ownership dropdown")
                        raise
                    time.sleep(1)
            
        except TimeoutException:
            print("Could not find vehicle ownership dropdown")
            raise HTTPException(
                status_code=404,
                detail="Vehicle ownership dropdown not found"
            )
        except StaleElementReferenceException:
            print("Vehicle ownership dropdown element became stale after max retries")
            raise HTTPException(
                status_code=500,
                detail="Vehicle ownership dropdown element became stale. Please try again."
            )
        
        # -------------------------------------------------------------------------
        # STEP 31: Select "Yes" for driver acknowledgment (always Yes)
        # -------------------------------------------------------------------------
        
        print("Looking for driver acknowledgment radio button...")
        
        try:
            # Find the "I've included everybody" radio button
            # Using XPath to find the ps-markdown with the acknowledgment text, then get the associated input
            driver_ack_radio = extended_wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//ps-markdown[contains(text(), \"I've included everybody that must be listed on this policy.\")]/ancestor::label/input[@type='radio']")
                )
            )
            print("Found driver acknowledgment radio button")
            
            # Scroll to the radio button
            driver.execute_script("arguments[0].scrollIntoView(true);", driver_ack_radio)
            time.sleep(1)
            
            # Click the radio button
            driver.execute_script("arguments[0].click();", driver_ack_radio)
            print("Selected: Yes - I've included everybody that must be listed on this policy")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After driver acknowledgment - Title: {driver.title}")
            print(f"After driver acknowledgment - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find driver acknowledgment radio button")
            raise HTTPException(
                status_code=404,
                detail="Driver acknowledgment radio button not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 32: Click the "Continue" button after driver acknowledgment
        # -------------------------------------------------------------------------
        
        print("Looking for 'Continue' button after driver acknowledgment...")
        
        try:
            # Find the Continue button by its data-pgr-id attribute
            continue_button = extended_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
            )
            print("Found 'Continue' button")
            
            # Scroll to the button
            driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
            time.sleep(1)
            
            # Click the Continue button
            driver.execute_script("arguments[0].click();", continue_button)
            print("Clicked 'Continue' button")
            
            # Wait for the new page to load
            time.sleep(5)
            
            print(f"After Continue click - Title: {driver.title}")
            print(f"After Continue click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find 'Continue' button after driver acknowledgment")
            raise HTTPException(
                status_code=404,
                detail="Continue button not found after driver acknowledgment"
            )
        
        # -------------------------------------------------------------------------
        # STEP 33: Select comprehensive deductible from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for comprehensive deductible dropdown...")
        print(f"Comprehensive deductible to select: {request.comprehensive_deductible}")
        
        # Map comprehensive deductible text to dropdown values
        comp_deductible_map = {
            "NO COVERAGE": "210100",
            "$100 DEDUCTIBLE": "210104",
            "$250 DEDUCTIBLE": "210106",
            "$500 DEDUCTIBLE": "210108",
            "$750 DEDUCTIBLE": "210109",
            "$1,000 DEDUCTIBLE": "210110",
            "$1000 DEDUCTIBLE": "210110",
            "$1,500 DEDUCTIBLE": "210130",
            "$1500 DEDUCTIBLE": "210130",
            "$2,000 DEDUCTIBLE": "210144",
            "$2000 DEDUCTIBLE": "210144",
            "$100 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210121",
            "$250 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210123",
            "$500 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210124",
            "$750 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210127",
            "$1,000 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210125",
            "$1000 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210125",
            "$1,500 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210188",
            "$1500 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210188",
            "$2,000 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210178",
            "$2000 DEDUCTIBLE WITH $0 GLASS DEDUCTIBLE": "210178"
        }
        
        comp_deductible_upper = request.comprehensive_deductible.upper().strip()
        comp_deductible_value = comp_deductible_map.get(comp_deductible_upper)
        
        if not comp_deductible_value:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid comprehensive_deductible: {request.comprehensive_deductible}. Must be one of the valid deductible options"
            )
        
        try:
            # Find the comprehensive deductible dropdown by its data-pgr-id attribute
            comp_deductible_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlCOMPLineCoverageLimit']"))
            )
            print("Found comprehensive deductible dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", comp_deductible_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", comp_deductible_dropdown)
            driver.execute_script("arguments[0].click();", comp_deductible_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, comp_deductible_dropdown, comp_deductible_value)
            
            print(f"Selected comprehensive deductible: {request.comprehensive_deductible} (value: {comp_deductible_value})")
            
            # Verify selection
            selected_value = comp_deductible_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After comprehensive deductible selection - Title: {driver.title}")
            print(f"After comprehensive deductible selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find comprehensive deductible dropdown")
            raise HTTPException(
                status_code=404,
                detail="Comprehensive deductible dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 34: Select medical payment coverage from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for medical payment coverage dropdown...")
        print(f"Medical payment coverage to select: {request.medical_payment_coverage}")
        
        # Map medical payment coverage text to dropdown values
        medpay_map = {
            "NO COVERAGE": "280100",
            "$500 EACH PERSON": "280191",
            "$1,000 EACH PERSON": "280192",
            "$1000 EACH PERSON": "280192",
            "$2,000 EACH PERSON": "280193",
            "$2000 EACH PERSON": "280193",
            "$5,000 EACH PERSON": "280194",
            "$5000 EACH PERSON": "280194",
            "$10,000 EACH PERSON": "280195",
            "$10000 EACH PERSON": "280195"
        }
        
        medpay_upper = request.medical_payment_coverage.upper().strip()
        medpay_value = medpay_map.get(medpay_upper)
        
        if not medpay_value:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid medical_payment_coverage: {request.medical_payment_coverage}. Must be one of: No Coverage, $500 each person, $1,000 each person, $2,000 each person, $5,000 each person, $10,000 each person"
            )
        
        try:
            # Find the medical payment coverage dropdown by its data-pgr-id attribute
            medpay_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlMEDPAYLineCoverageLimit']"))
            )
            print("Found medical payment coverage dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", medpay_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", medpay_dropdown)
            driver.execute_script("arguments[0].click();", medpay_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, medpay_dropdown, medpay_value)
            
            print(f"Selected medical payment coverage: {request.medical_payment_coverage} (value: {medpay_value})")
            
            # Verify selection
            selected_value = medpay_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After medical payment coverage selection - Title: {driver.title}")
            print(f"After medical payment coverage selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find medical payment coverage dropdown")
            raise HTTPException(
                status_code=404,
                detail="Medical payment coverage dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 35: Select collision deductible from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for collision deductible dropdown...")
        print(f"Collision deductible to select: {request.collision_deductible}")
        
        # Map collision deductible text to dropdown values
        collision_map = {
            "NO COVERAGE": "210300",
            "$100 DEDUCTIBLE": "210303",
            "$250 DEDUCTIBLE": "210304",
            "$500 DEDUCTIBLE": "210307",
            "$750 DEDUCTIBLE": "210310",
            "$1,000 DEDUCTIBLE": "210308",
            "$1000 DEDUCTIBLE": "210308",
            "$1,500 DEDUCTIBLE": "210323",
            "$1500 DEDUCTIBLE": "210323",
            "$2,000 DEDUCTIBLE": "210324",
            "$2000 DEDUCTIBLE": "210324"
        }
        
        collision_upper = request.collision_deductible.upper().strip()
        collision_value = collision_map.get(collision_upper)
        
        if not collision_value:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid collision_deductible: {request.collision_deductible}. Must be one of: No Coverage, $100 deductible, $250 deductible, $500 deductible, $750 deductible, $1,000 deductible, $1,500 deductible, $2,000 deductible"
            )
        
        try:
            # Find the collision deductible dropdown by its data-pgr-id attribute
            collision_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlCOLLLineCoverageLimit']"))
            )
            print("Found collision deductible dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", collision_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", collision_dropdown)
            driver.execute_script("arguments[0].click();", collision_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, collision_dropdown, collision_value)
            
            print(f"Selected collision deductible: {request.collision_deductible} (value: {collision_value})")
            
            # Verify selection
            selected_value = collision_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After collision deductible selection - Title: {driver.title}")
            print(f"After collision deductible selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find collision deductible dropdown")
            raise HTTPException(
                status_code=404,
                detail="Collision deductible dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 36: Select bodily injury and property damage liability from dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for bodily injury and property damage liability dropdown...")
        print(f"Bodily injury and property damage to select: {request.bodily_injury_property_damage}")
        
        # Map bodily injury and property damage text to dropdown values
        bipd_map = {
            "$15,000 EACH PERSON/$30,000 EACH ACCIDENT/$25,000 EACH ACCIDENT": "191003-200103",
            "$15000 EACH PERSON/$30000 EACH ACCIDENT/$25000 EACH ACCIDENT": "191003-200103",
            "$15,000 EACH PERSON/$30,000 EACH ACCIDENT/$50,000 EACH ACCIDENT": "191003-200105",
            "$15000 EACH PERSON/$30000 EACH ACCIDENT/$50000 EACH ACCIDENT": "191003-200105",
            "$25,000 EACH PERSON/$50,000 EACH ACCIDENT/$25,000 EACH ACCIDENT": "191005-200103",
            "$25000 EACH PERSON/$50000 EACH ACCIDENT/$25000 EACH ACCIDENT": "191005-200103",
            "$25,000 EACH PERSON/$50,000 EACH ACCIDENT/$50,000 EACH ACCIDENT": "191005-200105",
            "$25000 EACH PERSON/$50000 EACH ACCIDENT/$50000 EACH ACCIDENT": "191005-200105",
            "$50,000 EACH PERSON/$100,000 EACH ACCIDENT/$25,000 EACH ACCIDENT": "191006-200103",
            "$50000 EACH PERSON/$100000 EACH ACCIDENT/$25000 EACH ACCIDENT": "191006-200103",
            "$50,000 EACH PERSON/$100,000 EACH ACCIDENT/$50,000 EACH ACCIDENT": "191006-200105",
            "$50000 EACH PERSON/$100000 EACH ACCIDENT/$50000 EACH ACCIDENT": "191006-200105",
            "$100,000 EACH PERSON/$300,000 EACH ACCIDENT/$50,000 EACH ACCIDENT": "191008-200105",
            "$100000 EACH PERSON/$300000 EACH ACCIDENT/$50000 EACH ACCIDENT": "191008-200105",
            "$100,000 EACH PERSON/$300,000 EACH ACCIDENT/$100,000 EACH ACCIDENT": "191008-200106",
            "$100000 EACH PERSON/$300000 EACH ACCIDENT/$100000 EACH ACCIDENT": "191008-200106",
            "$250,000 EACH PERSON/$500,000 EACH ACCIDENT/$100,000 EACH ACCIDENT": "191015-200106",
            "$250000 EACH PERSON/$500000 EACH ACCIDENT/$100000 EACH ACCIDENT": "191015-200106",
            "$100,000 COMBINED SINGLE LIMIT": "191052-200152",
            "$100000 COMBINED SINGLE LIMIT": "191052-200152",
            "$300,000 COMBINED SINGLE LIMIT": "191053-200153",
            "$300000 COMBINED SINGLE LIMIT": "191053-200153",
            "$500,000 COMBINED SINGLE LIMIT": "191054-200154",
            "$500000 COMBINED SINGLE LIMIT": "191054-200154"
        }
        
        bipd_upper = request.bodily_injury_property_damage.upper().strip()
        bipd_value = bipd_map.get(bipd_upper)
        
        if not bipd_value:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid bodily_injury_property_damage: {request.bodily_injury_property_damage}. Must be a valid coverage option (e.g., '$100,000 each person/$300,000 each accident/$100,000 each accident' or '$300,000 combined single limit')"
            )
        
        try:
            # Find the bodily injury and property damage dropdown by its data-pgr-id attribute
            bipd_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlBIPDLineCoverageLimit']"))
            )
            print("Found bodily injury and property damage liability dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bipd_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", bipd_dropdown)
            driver.execute_script("arguments[0].click();", bipd_dropdown)
            time.sleep(1)
            
            # Use JavaScript to set the value and trigger change events
            driver.execute_script("""
                var select = arguments[0];
                select.value = arguments[1];
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
            """, bipd_dropdown, bipd_value)
            
            print(f"Selected bodily injury and property damage: {request.bodily_injury_property_damage} (value: {bipd_value})")
            
            # Verify selection
            selected_value = bipd_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After bodily injury and property damage selection - Title: {driver.title}")
            print(f"After bodily injury and property damage selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find bodily injury and property damage liability dropdown")
            raise HTTPException(
                status_code=404,
                detail="Bodily injury and property damage liability dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 37: Select second option (No Coverage) from uninsured/underinsured motorist dropdown
        # -------------------------------------------------------------------------
        
        print("Looking for uninsured/underinsured motorist coverage dropdown...")
        print("Selecting second option: No Coverage")
        
        try:
            # Find the UM/UIM dropdown by its data-pgr-id attribute
            umuim_dropdown = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[data-pgr-id='ddlUMUIMLineCoverageLimit']"))
            )
            print("Found uninsured/underinsured motorist coverage dropdown")
            
            # Scroll to the dropdown with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", umuim_dropdown)
            time.sleep(1)
            
            # Use JavaScript click to avoid interception by sticky headers
            driver.execute_script("arguments[0].focus();", umuim_dropdown)
            driver.execute_script("arguments[0].click();", umuim_dropdown)
            time.sleep(1)
            
            # Select the second option by index (No Coverage - index 1)
            umuim_select = Select(umuim_dropdown)
            umuim_select.select_by_index(1)
            
            print("Selected second option: No Coverage")
            
            # Verify selection
            selected_value = umuim_dropdown.get_attribute('value')
            print(f"Verified selected value: {selected_value}")
            
            # Wait a moment for the selection to register
            time.sleep(2)
            
            print(f"After UM/UIM selection - Title: {driver.title}")
            print(f"After UM/UIM selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find uninsured/underinsured motorist coverage dropdown")
            raise HTTPException(
                status_code=404,
                detail="Uninsured/underinsured motorist coverage dropdown not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 38: Click "Continue" button after coverage selections
        # -------------------------------------------------------------------------
        
        print("Looking for Continue button after coverage selections...")
        
        try:
            # Find the Continue button by its data-pgr-id attribute
            continue_button = extended_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
            )
            print("Found Continue button")
            
            # Scroll to the button with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", continue_button)
            time.sleep(1)
            
            # Click the Continue button using JavaScript to avoid interception
            driver.execute_script("arguments[0].click();", continue_button)
            print("Clicked Continue button")
            
            # Wait for page to load after clicking Continue
            time.sleep(3)
            
            print(f"After Continue click - Title: {driver.title}")
            print(f"After Continue click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find Continue button after coverage selections")
            raise HTTPException(
                status_code=404,
                detail="Continue button not found after coverage selections"
            )
        
        # -------------------------------------------------------------------------
        # STEP 39: Scrape final page data (Replace vehicle, Premium details)
        # -------------------------------------------------------------------------
        
        print("Scraping final page data...")
        
        try:
            # Wait for the final review page to fully load
            time.sleep(3)
            
            # Scrape Replace Vehicle field
            replace_vehicle_text = ""
            try:
                replace_vehicle_element = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#transaction-messaging ps-markdown"))
                )
                replace_vehicle_text = replace_vehicle_element.text.strip()
                print(f"Replace vehicle: {replace_vehicle_text}")
            except TimeoutException:
                print("Could not find replace vehicle field")
                replace_vehicle_text = "Not found"
            
            # Scrape Total Premium Increase
            total_premium_increase = ""
            try:
                # Find h4 element containing "Total premium increase:"
                premium_increase_elements = driver.find_elements(By.CSS_SELECTOR, "h4.f5-e.fwi.ma0")
                for element in premium_increase_elements:
                    if "Total premium increase:" in element.text:
                        # Extract just the amount (e.g., "$792.52")
                        total_premium_increase = element.text.replace("Total premium increase:", "").strip()
                        print(f"Total premium increase: {total_premium_increase}")
                        break
                if not total_premium_increase:
                    total_premium_increase = "Not found"
            except Exception as e:
                print(f"Could not find total premium increase: {str(e)}")
                total_premium_increase = "Not found"
            
            # Scrape New Policy Premium
            new_policy_premium = ""
            try:
                new_premium_element = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-pgr-id='txtNewPremium'] span.review-item-embed"))
                )
                new_policy_premium = new_premium_element.text.strip()
                print(f"New policy premium: {new_policy_premium}")
            except TimeoutException:
                print("Could not find new policy premium field")
                new_policy_premium = "Not found"
            
            # Scrape Policy Start Date
            policy_start_date = ""
            try:
                start_date_element = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li[data-pgr-id='txtStartsOn'] span"))
                )
                policy_start_date = start_date_element.text.strip()
                print(f"Policy starts on: {policy_start_date}")
            except TimeoutException:
                print("Could not find policy start date field")
                policy_start_date = "Not found"
            
            # Scrape New Premium Description
            new_premium_description = ""
            try:
                premium_description_element = extended_wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "pui-p[data-pgr-id='msgInternalMessage0'] p"))
                )
                new_premium_description = premium_description_element.text.strip()
                print(f"New premium description: {new_premium_description}")
            except TimeoutException:
                print("Could not find new premium description field")
                new_premium_description = "Not found"
            
            print("=" * 60)
            print("✅ Step 39 completed successfully!")
            print(f"✅ Scraped all final page data")
            print("=" * 60)
            
        except Exception as e:
            print(f"Error scraping final page data: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Error scraping final page data: {str(e)}"
            )
        
        # -------------------------------------------------------------------------
        # STEP 40: Click "View upcoming payments" link and scrape payment schedule
        # -------------------------------------------------------------------------
        
        print("Looking for 'View upcoming payments' link...")
        
        try:
            # Find and click the "View upcoming payments" link
            view_payments_link = extended_wait.until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'View upcoming payments')]"))
            )
            print("Found 'View upcoming payments' link")
            
            # Scroll to the link
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", view_payments_link)
            time.sleep(1)
            
            # Click the link using JavaScript
            driver.execute_script("arguments[0].click();", view_payments_link)
            print("Clicked 'View upcoming payments' link")
            
            # Wait for the popup/modal to appear
            time.sleep(2)
            
            # Wait for the payment schedule table to be visible
            payment_table = extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table[data-pgr-id='tblPaymentSchedule']"))
            )
            print("Payment schedule table loaded")
            
            # Scrape the payment schedule table rows
            payment_schedule = []
            table_rows = driver.find_elements(By.CSS_SELECTOR, "table[data-pgr-id='tblPaymentSchedule'] tbody tr")
            
            print(f"Found {len(table_rows)} payment schedule rows")
            
            for index, row in enumerate(table_rows):
                try:
                    # Extract all td elements from the row
                    cells = row.find_elements(By.TAG_NAME, "td")
                    
                    if len(cells) >= 4:
                        # Extract date (from the span inside first td)
                        date_element = cells[0].find_element(By.TAG_NAME, "span")
                        date = date_element.text.strip()
                        
                        # Extract current amount
                        current_amount = cells[1].text.strip()
                        
                        # Extract new amount
                        new_amount = cells[2].text.strip()
                        
                        # Extract difference
                        difference = cells[3].text.strip()
                        
                        payment_schedule.append({
                            "date": date,
                            "current_amount": current_amount,
                            "new_amount": new_amount,
                            "difference": difference
                        })
                        
                        print(f"Row {index + 1}: {date} | Current: {current_amount} | New: {new_amount} | Diff: {difference}")
                        
                except Exception as e:
                    print(f"Error parsing row {index + 1}: {str(e)}")
                    continue
            
            # Scrape the installment fee note
            installment_fee_note = ""
            try:
                fee_note_element = driver.find_element(By.CSS_SELECTOR, "pui-p[data-pgr-id='ttlServiceChargeDescription'] p")
                installment_fee_note = fee_note_element.text.strip()
                print(f"Installment fee note: {installment_fee_note}")
            except Exception as e:
                print(f"Could not find installment fee note: {str(e)}")
                installment_fee_note = "Not found"
            
            # Close the payment schedule popup
            try:
                close_button = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Close Modal']")
                print("Found close button for payment schedule popup")
                
                # Click the close button using JavaScript
                driver.execute_script("arguments[0].click();", close_button)
                print("Clicked close button - popup closed")
                
                # Wait for popup to close
                time.sleep(1)
                
            except Exception as e:
                print(f"Could not find or click close button: {str(e)}")
            
            print("=" * 60)
            print("✅ Step 40 completed successfully!")
            print(f"✅ Scraped {len(payment_schedule)} payment schedule entries")
            print("=" * 60)
            
        except TimeoutException:
            print("Could not find 'View upcoming payments' link or payment schedule table")
            raise HTTPException(
                status_code=404,
                detail="View upcoming payments link or payment schedule table not found"
            )
        except Exception as e:
            print(f"Error scraping payment schedule: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Error scraping payment schedule: {str(e)}"
            )
        
        # -------------------------------------------------------------------------
        # STEP 41: Click "effect on rate for the entire policy period" link and scrape coverage comparison data
        # -------------------------------------------------------------------------
        
        print("Looking for 'effect on rate for the entire policy period' link...")
        
        try:
            # Find and click the effect on rate link
            effect_on_rate_link = extended_wait.until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'effect on rate for the entire policy period')]"))
            )
            print("Found 'effect on rate for the entire policy period' link")
            
            # Scroll to the link
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", effect_on_rate_link)
            time.sleep(1)
            
            # Click the link using JavaScript
            driver.execute_script("arguments[0].click();", effect_on_rate_link)
            print("Clicked 'effect on rate for the entire policy period' link")
            
            # Wait for the modal to appear
            time.sleep(2)
            
            # Wait for the modal content to be visible
            extended_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "pui-modal-body"))
            )
            print("Effect on rate modal loaded")
            
            # Initialize data structure for storing all scraped data
            effect_on_rate_data = {
                "vehicle_summary": [],
                "total_policy_rate": {},
                "vehicle_details": []
            }
            
            # Scrape Vehicle Summary section (top section with vehicle totals)
            print("Scraping vehicle summary section...")
            try:
                vehicle_summary_elements = driver.find_elements(By.XPATH, "//pui-h3[contains(text(), 'Vehicle')]/following-sibling::div//pui-p[@fw='7']")
                
                for vehicle_elem in vehicle_summary_elements:
                    vehicle_name = vehicle_elem.text.strip()
                    
                    # Find the corresponding table for this vehicle
                    parent_div = vehicle_elem.find_element(By.XPATH, "./ancestor::div[contains(@class, 'ng-star-inserted')]")
                    table_rows = parent_div.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    
                    if len(table_rows) > 0:
                        cells = table_rows[0].find_elements(By.TAG_NAME, "td")
                        if len(cells) >= 2:
                            current_value = cells[0].text.strip()
                            new_value = cells[1].text.strip()
                            
                            effect_on_rate_data["vehicle_summary"].append({
                                "vehicle_name": vehicle_name,
                                "current_rate": current_value,
                                "new_rate": new_value
                            })
                            print(f"Vehicle: {vehicle_name} | Current: {current_value} | New: {new_value}")
                            
            except Exception as e:
                print(f"Error scraping vehicle summary: {str(e)}")
            
            # Scrape Total Policy Rate section
            print("Scraping total policy rate...")
            try:
                total_policy_elements = driver.find_elements(By.XPATH, "//pui-p[contains(text(), 'Total Policy Rate')]")
                
                if total_policy_elements:
                    parent_div = total_policy_elements[0].find_element(By.XPATH, "./ancestor::div[contains(@class, 'ng-star-inserted')]")
                    table_rows = parent_div.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    
                    if len(table_rows) > 0:
                        cells = table_rows[0].find_elements(By.TAG_NAME, "td")
                        if len(cells) >= 2:
                            current_value = cells[0].text.strip()
                            new_value = cells[1].text.strip()
                            
                            effect_on_rate_data["total_policy_rate"] = {
                                "current_rate": current_value,
                                "new_rate": new_value
                            }
                            print(f"Total Policy Rate | Current: {current_value} | New: {new_value}")
                            
            except Exception as e:
                print(f"Error scraping total policy rate: {str(e)}")
            
            # Scrape detailed vehicle breakdowns (after the hr separator)
            print("Scraping detailed vehicle breakdowns...")
            try:
                # Find all vehicle headers (h4 elements with vehicle names)
                vehicle_headers = driver.find_elements(By.XPATH, "//pui-hr/following-sibling::pui-h4[@class='db f4 fw6 lh-title mv2 outline-0 pgr-dark-blue ng-star-inserted']")
                
                for vehicle_header in vehicle_headers:
                    vehicle_name = vehicle_header.text.strip()
                    print(f"Processing detailed breakdown for: {vehicle_name}")
                    
                    vehicle_data = {
                        "vehicle_name": vehicle_name,
                        "coverages": []
                    }
                    
                    # Find all coverage sections for this vehicle
                    # Look for pui-p elements with fw="7" that follow this h4
                    current_element = vehicle_header
                    
                    while True:
                        try:
                            # Find the next sibling div
                            next_sibling = current_element.find_element(By.XPATH, "./following-sibling::div[1]")
                            
                            # Check if this is another vehicle header (stop if so)
                            try:
                                next_header = next_sibling.find_element(By.XPATH, "./preceding-sibling::pui-h4[1]")
                                if next_header != vehicle_header:
                                    break
                            except:
                                pass
                            
                            # Try to find coverage name in this div
                            try:
                                coverage_name_elem = next_sibling.find_element(By.CSS_SELECTOR, "pui-p[fw='7'] p span")
                                coverage_name = coverage_name_elem.text.strip()
                                
                                # Find the table in this div
                                table = next_sibling.find_element(By.CSS_SELECTOR, "table")
                                table_rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
                                
                                # Extract current and new values
                                current_coverage = ""
                                current_value = ""
                                new_coverage = ""
                                new_value = ""
                                
                                for row_index, row in enumerate(table_rows):
                                    cells = row.find_elements(By.TAG_NAME, "td")
                                    
                                    if row_index == 0 and len(cells) >= 2:
                                        # First row contains coverage details
                                        current_coverage = cells[0].text.strip()
                                        new_coverage = cells[1].text.strip()
                                    elif row_index == 1 and len(cells) >= 2:
                                        # Second row contains values
                                        current_value = cells[0].text.strip()
                                        new_value = cells[1].text.strip()
                                
                                vehicle_data["coverages"].append({
                                    "coverage_name": coverage_name,
                                    "current_coverage": current_coverage,
                                    "current_value": current_value,
                                    "new_coverage": new_coverage,
                                    "new_value": new_value
                                })
                                
                                print(f"  - {coverage_name}: Current ${current_value} -> New ${new_value}")
                                
                            except:
                                pass
                            
                            current_element = next_sibling
                            
                        except:
                            break
                    
                    effect_on_rate_data["vehicle_details"].append(vehicle_data)
                    
            except Exception as e:
                print(f"Error scraping vehicle details: {str(e)}")
            
            # Close the effect on rate modal
            try:
                close_button = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Close Modal']")
                print("Found close button for effect on rate modal")
                
                # Scroll to button
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", close_button)
                time.sleep(1)
                
                # Click the close button using JavaScript
                driver.execute_script("arguments[0].click();", close_button)
                print("Clicked close button - effect on rate modal closed")
                
                # Wait for modal to close
                time.sleep(1)
                
            except Exception as e:
                print(f"Could not find or click close button: {str(e)}")
            
            print("=" * 60)
            print("✅ Step 41 completed successfully!")
            print(f"✅ Scraped effect on rate data for {len(effect_on_rate_data['vehicle_details'])} vehicles")
            print("=" * 60)
            
        except TimeoutException:
            print("Could not find 'effect on rate for the entire policy period' link or modal")
            raise HTTPException(
                status_code=404,
                detail="Effect on rate link or modal not found"
            )
        except Exception as e:
            print(f"Error scraping effect on rate data: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Error scraping effect on rate data: {str(e)}"
            )
        
        # -------------------------------------------------------------------------
        # STEP 42: Click "Save this update for later" checkbox
        # -------------------------------------------------------------------------
        
        print("Looking for 'Save this update for later' checkbox...")
        
        try:
            # Find the checkbox/radio option for "Save this update for later"
            save_for_later_option = extended_wait.until(
                EC.element_to_be_clickable((By.XPATH, "//ps-markdown[@data-pgr-id='lblSavethisupdateforlater' or contains(text(), 'Save this update for later')]"))
            )
            print("Found 'Save this update for later' option")
            
            # Scroll to the option
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", save_for_later_option)
            time.sleep(1)
            
            # Click the option using JavaScript
            driver.execute_script("arguments[0].click();", save_for_later_option)
            print("Clicked 'Save this update for later' option")
            
            # Wait for selection to register
            time.sleep(2)
            
            print(f"After save for later selection - Title: {driver.title}")
            print(f"After save for later selection - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find 'Save this update for later' option")
            raise HTTPException(
                status_code=404,
                detail="Save this update for later option not found"
            )
        
        # -------------------------------------------------------------------------
        # STEP 43: Click final "Continue" button and wait for new page to load
        # -------------------------------------------------------------------------
        
        print("Looking for final Continue button...")
        
        try:
            # Find the Continue button by its data-pgr-id attribute
            continue_button = extended_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-pgr-id='btnContinue']"))
            )
            print("Found final Continue button")
            
            # Scroll to the button with extra offset to avoid sticky headers
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", continue_button)
            time.sleep(1)
            
            # Click the Continue button using JavaScript to avoid interception
            driver.execute_script("arguments[0].click();", continue_button)
            print("Clicked final Continue button")
            
            # Wait 5 seconds for the new page to load
            print("Waiting 5 seconds for new page to load...")
            time.sleep(5)
            
            print(f"After final Continue click - Title: {driver.title}")
            print(f"After final Continue click - URL: {driver.current_url}")
            
        except TimeoutException:
            print("Could not find final Continue button")
            raise HTTPException(
                status_code=404,
                detail="Final Continue button not found"
            )
        
        # -------------------------------------------------------------------------
        # STOPPING POINT: Bot stops here after Step 43
        # Waiting for further instructions for next steps
        # -------------------------------------------------------------------------
        
        log_thread(thread_id, "=" * 60)
        log_thread(thread_id, "✅ Step 43 completed successfully!")
        log_thread(thread_id, "✅ Clicked final Continue button and waited for page to load")
        log_thread(thread_id, "=" * 60)
        
        # Update thread status
        with browser_threads_lock:
            if thread_id in browser_threads:
                browser_threads[thread_id]["status"] = "completed"
        
        # Prepare response data before cleanup
        response_data = {
            "success": True,
            "message": "Vehicle replacement process completed successfully",
            "policy_number": request.policy_no,
            "replace_vehicle": replace_vehicle_text,
            "total_premium_increase": total_premium_increase,
            "new_policy_premium": new_policy_premium,
            "policy_start_date": policy_start_date,
            "new_premium_description": new_premium_description,
            "payment_schedule": payment_schedule,
            "installment_fee_note": installment_fee_note,
            "effect_on_rate": effect_on_rate_data
        }
        
        log_thread(thread_id, "📦 Preparing to send response to client...")
        
        # Close browser before returning response to avoid hanging
        if driver:
            try:
                log_thread(thread_id, "🔧 Closing browser...")
                driver.quit()
                log_thread(thread_id, "✅ Browser closed successfully")
            except Exception as e:
                log_thread(thread_id, f"⚠️  Warning: Error closing browser: {str(e)}")
        
        log_thread(thread_id, "=" * 60)
        log_thread(thread_id, "✅ Sending response to client")
        log_thread(thread_id, "=" * 60)
        
        # Return scraped data
        return response_data
        
    except TimeoutException as e:
        log_thread(thread_id, f"❌ TimeoutException: {str(e)}")
        # Update thread status
        with browser_threads_lock:
            if thread_id in browser_threads:
                browser_threads[thread_id]["status"] = "timeout_error"
        if driver:
            try:
                driver.quit()
            except:
                pass
        raise HTTPException(
            status_code=408,
            detail=f"Timeout waiting for page elements: {str(e)}"
        )
        
    except NoSuchElementException as e:
        log_thread(thread_id, f"❌ NoSuchElementException: {str(e)}")
        # Update thread status
        with browser_threads_lock:
            if thread_id in browser_threads:
                browser_threads[thread_id]["status"] = "element_not_found_error"
        if driver:
            try:
                driver.quit()
            except:
                pass
        raise HTTPException(
            status_code=404,
            detail=f"Required element not found: {str(e)}"
        )
        
    except Exception as e:
        log_thread(thread_id, f"❌ Exception occurred: {str(e)}")
        # Update thread status
        with browser_threads_lock:
            if thread_id in browser_threads:
                browser_threads[thread_id]["status"] = "error"
                browser_threads[thread_id]["error"] = str(e)
        if driver:
            try:
                driver.quit()
            except:
                pass
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred: {str(e)}"
        )


@app.post("/start")
async def retrieve_policy(request: PolicyRequest):
    """
    Main endpoint to start the bot and navigate to policy details page.
    Runs the automation in a thread pool so multiple browsers can operate concurrently.
    
    Args:
        request: PolicyRequest containing all the request data
    
    Returns:
        dict: Success response with automation results
    """
    # Get unique thread ID for this browser instance
    thread_id = get_next_thread_id()
    
    # Update thread status
    with browser_threads_lock:
        if thread_id in browser_threads:
            browser_threads[thread_id]["status"] = "starting"
            browser_threads[thread_id]["policy_no"] = request.policy_no
            browser_threads[thread_id]["action_type"] = request.action_type
    
    # Log the start of the request with thread ID
    log_thread(thread_id, "=" * 80)
    log_thread(thread_id, "🚀 START ENDPOINT HIT - Beginning policy retrieval process")
    log_thread(thread_id, "=" * 80)
    log_thread(thread_id, f"📋 Policy Number: {request.policy_no}")
    log_thread(thread_id, f"👤 Username: {request.username[:3]}***")  # Partial for security
    log_thread(thread_id, f"🎯 Action Type: {request.action_type}")
    if "driver" in request.action_type.lower():
        log_thread(thread_id, f"📅 Effective Date: {request.date_to_add_driver}")
    else:
        if request.vehicle_name_to_replace:
            log_thread(thread_id, f"🚗 Existing Vehicle: {request.vehicle_name_to_replace}")
        else:
            log_thread(thread_id, f"🚗 Existing Vehicle: N/A (Add vehicle)")
        log_thread(thread_id, f"📅 Effective Date: {request.date_to_rep_vehical}")
    log_thread(thread_id, "=" * 80)
    
    # Run the ENTIRE automation in a thread pool executor
    # This allows multiple browsers to run concurrently without blocking each other
    loop = asyncio.get_event_loop()
    
    try:
        # Execute automation in thread pool - each browser runs independently
        result = await loop.run_in_executor(None, run_automation_sync, request, thread_id)
        return result
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        log_thread(thread_id, f"❌ Outer exception: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred: {str(e)}"
        )


@app.post("/otp")
async def send_otp(request: dict):
    """
    API endpoint to receive OTP from external source (Twilio webhook or manual input)
    
    Args:
        request: Dictionary containing 'otp' field
        
    Returns:
        dict: Success response with OTP confirmation
    """
    print(f"📨 OTP endpoint hit at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📦 Request data: {request}")
    
    try:
        # Handle both JSON and form-encoded data
        if isinstance(request, dict):
            # JSON format (for manual testing)
            otp_code = request.get('otp')
        else:
            # Form-encoded format (Twilio webhook)
            form_data = request
            sms_body = form_data.get('Body', '')
            
            print(f"📱 Received SMS body: {sms_body}")
            
            # Extract OTP from SMS body using regex
            otp_match = re.search(r'(\d{6})', sms_body)  # Look for 6-digit code
            if otp_match:
                otp_code = otp_match.group(1)
                print(f"🔍 Extracted OTP from SMS: {otp_code}")
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Could not extract OTP from SMS body"
                )
        
        if not otp_code:
            raise HTTPException(
                status_code=400,
                detail="OTP code is required"
            )
        
        # Add OTP to queue for FIFO distribution to browsers
        with otp_queue_lock:
            otp_queue.put(str(otp_code))
            queue_size = otp_queue.qsize()
        
        # Check which threads are waiting for OTP
        waiting_threads = []
        with browser_threads_lock:
            for tid, info in browser_threads.items():
                if info.get("status") in ["waiting_for_otp", "browser_initialized"]:
                    waiting_threads.append(tid)
        
        # Also store in legacy global storage for backward compatibility
        otp_storage["otp"] = str(otp_code)
        otp_storage["timestamp"] = time.time()
        
        # Return response immediately - don't wait for anything
        response_data = {
            "success": True,
            "message": "OTP received successfully",
            "otp": otp_code,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # Log after preparing response to minimize blocking
        print(f"✅ OTP received via API: {otp_code}")
        print(f"📬 OTP added to queue (queue size: {queue_size}, waiting threads: {waiting_threads})")
        if waiting_threads:
            print(f"📋 Next OTP will go to Thread-{waiting_threads[0]} (FIFO order)")
        print(f"⏱️  OTP endpoint processing complete")
        
        # Return immediately - this endpoint should be fast
        return response_data
        
    except Exception as e:
        print(f"❌ Error in OTP endpoint: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error processing OTP: {str(e)}"
        )


@app.get("/otp/status")
async def otp_status():
    """
    Check if OTP is waiting
    
    Returns:
        dict: OTP status information
    """
    try:
        if otp_storage["otp"] is not None:
            age = time.time() - otp_storage["timestamp"]
            return {
                "waiting": True,
                "age_seconds": int(age),
                "expires_in": int(300 - age) if age < 300 else 0
            }
        else:
            return {"waiting": False}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error checking OTP status: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    import sys
    
    # Railway automatically sets PORT env variable (typically 8080)
    # Default to 8080 for Railway compatibility, but allow override
    port = int(os.environ.get('PORT', 8080))
    
    print("=" * 60)
    print("🚀 Progressive Driver Add/Update Bot - Starting Up")
    print("=" * 60)
    print(f"📡 Port: {port}")
    print(f"🌐 Host: 0.0.0.0")
    print(f"🐍 Python: {sys.version}")
    print(f"📁 Working Directory: {os.getcwd()}")
    print(f"💡 Local URL: http://localhost:{port}")
    print(f"💡 Health Check: http://localhost:{port}/health")
    print(f"💡 API Endpoint: http://localhost:{port}/start")
    print("=" * 60)
    
    try:
        uvicorn.run(
            app, 
            host="0.0.0.0", 
            port=port,
            log_level="info",
            access_log=False  # Disabled - we use custom middleware logging instead
        )
    except Exception as e:
        print(f"❌ Server failed to start: {str(e)}")
        raise

