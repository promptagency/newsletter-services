#!/bin/bash

# Scraper Service Management Script
SERVICE_NAME="scraper"

show_help() {
    echo "Scraper Service Manager"
    echo ""
    echo "Usage: ./manage_scraper.sh [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  status    Show service status"
    echo "  start     Start the service"
    echo "  stop      Stop the service"
    echo "  restart   Restart the service"
    echo "  logs      Show recent logs"
    echo "  follow    Follow live logs (Ctrl+C to exit)"
    echo "  test      Test service endpoints"
    echo "  health    Quick health check"
    echo "  enable    Enable service to start on boot"
    echo "  disable   Disable service from starting on boot"
    echo "  uninstall Remove the service completely"
    echo ""
}

check_service_exists() {
    if ! systemctl list-unit-files | grep -q "^$SERVICE_NAME.service"; then
        echo "❌ Scraper service is not installed."
        echo "Run the installation script first: sudo ./install_scraper_service.sh"
        exit 1
    fi
}

test_endpoints() {
    echo "Testing Scraper endpoints..."
    echo ""
    
    # Test cache status endpoint
    echo "1. Cache status check:"
    if response=$(curl -s http://localhost:5001/cache/status 2>/dev/null); then
        echo "✅ Cache status endpoint responding"
        echo "Response: $response"
    else
        echo "❌ Cache status endpoint not responding"
        return 1
    fi
    
    echo ""
    
    # Test extraction endpoint with a simple URL
    echo "2. Testing content extraction:"
    echo "   (This may take a few seconds...)"
    if response=$(curl -s -X POST http://localhost:5001/extract \
        -H "Content-Type: application/json" \
        -d '{"url":"https://example.com"}' 2>/dev/null); then
        
        if echo "$response" | grep -q '"success":true'; then
            echo "✅ Extraction endpoint working"
            echo "Response preview: $(echo "$response" | head -c 200)..."
        else
            echo "⚠️  Extraction endpoint responded but extraction may have failed"
            echo "Response: $response"
        fi
    else
        echo "❌ Extraction endpoint not responding"
        return 1
    fi
    
    echo ""
    echo "✅ All endpoints are working!"
}

quick_health() {
    if curl -s http://localhost:5001/cache/status > /dev/null 2>&1; then
        echo "✅ Scraper service is healthy and responding"
    else
        echo "❌ Scraper service is not responding"
        echo "Check status with: ./manage_scraper.sh status"
        exit 1
    fi
}

case "$1" in
    "status")
        check_service_exists
        echo "=== Scraper Service Status ==="
        systemctl status $SERVICE_NAME.service --no-pager -l
        ;;
    
    "start")
        check_service_exists
        echo "Starting Scraper service..."
        sudo systemctl start $SERVICE_NAME.service
        sleep 2
        systemctl is-active --quiet $SERVICE_NAME.service && echo "✅ Service started successfully" || echo "❌ Failed to start service"
        ;;
    
    "stop")
        check_service_exists
        echo "Stopping Scraper service..."
        sudo systemctl stop $SERVICE_NAME.service
        sleep 1
        systemctl is-active --quiet $SERVICE_NAME.service && echo "❌ Service still running" || echo "✅ Service stopped successfully"
        ;;
    
    "restart")
        check_service_exists
        echo "Restarting Scraper service..."
        sudo systemctl restart $SERVICE_NAME.service
        sleep 3
        systemctl is-active --quiet $SERVICE_NAME.service && echo "✅ Service restarted successfully" || echo "❌ Failed to restart service"
        ;;
    
    "logs")
        check_service_exists
        echo "=== Recent Scraper Logs ==="
        sudo journalctl -u $SERVICE_NAME.service --no-pager -l -n 50
        ;;
    
    "follow")
        check_service_exists
        echo "=== Following Scraper Logs (Ctrl+C to exit) ==="
        sudo journalctl -u $SERVICE_NAME.service -f
        ;;
    
    "test")
        test_endpoints
        ;;
    
    "health")
        quick_health
        ;;
    
    "enable")
        check_service_exists
        echo "Enabling Scraper service to start on boot..."
        sudo systemctl enable $SERVICE_NAME.service
        echo "✅ Service will now start automatically on boot"
        ;;
    
    "disable")
        check_service_exists
        echo "Disabling Scraper service from starting on boot..."
        sudo systemctl disable $SERVICE_NAME.service
        echo "✅ Service will no longer start automatically on boot"
        ;;
    
    "uninstall")
        check_service_exists
        echo "⚠️  This will completely remove the Scraper service."
        read -p "Are you sure? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "Stopping and removing Scraper service..."
            sudo systemctl stop $SERVICE_NAME.service 2>/dev/null
            sudo systemctl disable $SERVICE_NAME.service 2>/dev/null
            sudo rm -f /etc/systemd/system/$SERVICE_NAME.service
            sudo systemctl daemon-reload
            echo "✅ Scraper service has been uninstalled"
        else
            echo "Cancelled."
        fi
        ;;
    
    *)
        show_help
        ;;
esac
