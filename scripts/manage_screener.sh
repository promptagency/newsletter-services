#!/bin/bash

# Screener Service Management Script
SERVICE_NAME="screener"

show_help() {
    echo "Screener Service Manager"
    echo ""
    echo "Usage: ./manage_screener.sh [COMMAND]"
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
        echo "❌ Screener service is not installed."
        echo "Run the installation script first: sudo ./install_screener_service.sh"
        exit 1
    fi
}

test_endpoints() {
    echo "Testing Screener endpoints..."
    echo ""
    
    # Test health endpoint
    echo "1. Health check:"
    if response=$(curl -s http://localhost:5002/health 2>/dev/null); then
        echo "✅ Health endpoint responding"
        echo "Response: $response"
    else
        echo "❌ Health endpoint not responding"
        return 1
    fi
    
    echo ""
    
    # Test status endpoint  
    echo "2. Status check:"
    if response=$(curl -s http://localhost:5002/status 2>/dev/null); then
        echo "✅ Status endpoint responding"
        echo "Response: $response"
    else
        echo "❌ Status endpoint not responding"
        return 1
    fi
    
    echo ""
    echo "✅ All endpoints are working!"
}

quick_health() {
    if curl -s http://localhost:5002/health > /dev/null 2>&1; then
        echo "✅ Screener service is healthy and responding"
    else
        echo "❌ Screener service is not responding"
        echo "Check status with: ./manage_screener.sh status"
        exit 1
    fi
}

case "$1" in
    "status")
        check_service_exists
        echo "=== Screener Service Status ==="
        systemctl status $SERVICE_NAME.service --no-pager -l
        ;;
    
    "start")
        check_service_exists
        echo "Starting Screener service..."
        sudo systemctl start $SERVICE_NAME.service
        sleep 2
        systemctl is-active --quiet $SERVICE_NAME.service && echo "✅ Service started successfully" || echo "❌ Failed to start service"
        ;;
    
    "stop")
        check_service_exists
        echo "Stopping Screener service..."
        sudo systemctl stop $SERVICE_NAME.service
        sleep 1
        systemctl is-active --quiet $SERVICE_NAME.service && echo "❌ Service still running" || echo "✅ Service stopped successfully"
        ;;
    
    "restart")
        check_service_exists
        echo "Restarting Screener service..."
        sudo systemctl restart $SERVICE_NAME.service
        sleep 3
        systemctl is-active --quiet $SERVICE_NAME.service && echo "✅ Service restarted successfully" || echo "❌ Failed to restart service"
        ;;
    
    "logs")
        check_service_exists
        echo "=== Recent Screener Logs ==="
        sudo journalctl -u $SERVICE_NAME.service --no-pager -l -n 50
        ;;
    
    "follow")
        check_service_exists
        echo "=== Following Screener Logs (Ctrl+C to exit) ==="
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
        echo "Enabling Screener service to start on boot..."
        sudo systemctl enable $SERVICE_NAME.service
        echo "✅ Service will now start automatically on boot"
        ;;
    
    "disable")
        check_service_exists
        echo "Disabling Screener service from starting on boot..."
        sudo systemctl disable $SERVICE_NAME.service
        echo "✅ Service will no longer start automatically on boot"
        ;;
    
    "uninstall")
        check_service_exists
        echo "⚠️  This will completely remove the Screener service."
        read -p "Are you sure? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "Stopping and removing Screener service..."
            sudo systemctl stop $SERVICE_NAME.service 2>/dev/null
            sudo systemctl disable $SERVICE_NAME.service 2>/dev/null
            sudo rm -f /etc/systemd/system/$SERVICE_NAME.service
            sudo systemctl daemon-reload
            echo "✅ Screener service has been uninstalled"
        else
            echo "Cancelled."
        fi
        ;;
    
    *)
        show_help
        ;;
esac
