/**
 * Authentication helper for ClinicWire
 * Handles JWT token management and auth state
 */

class AuthManager {
    constructor() {
        this.token = localStorage.getItem('clinicwire_token');
        this.user = this.getStoredUser();
        this.organizations = this.getStoredOrganizations();
        this.isDemo = localStorage.getItem('clinicwire_demo_mode') === 'true';
    }

    getStoredUser() {
        try {
            const userData = localStorage.getItem('clinicwire_user');
            return userData ? JSON.parse(userData) : null;
        } catch (e) {
            console.error('Failed to parse user data:', e);
            return null;
        }
    }

    getStoredOrganizations() {
        try {
            const orgsData = localStorage.getItem('clinicwire_organizations');
            return orgsData ? JSON.parse(orgsData) : [];
        } catch (e) {
            console.error('Failed to parse organizations data:', e);
            return [];
        }
    }

    isAuthenticated() {
        return !!this.token && !!this.user;
    }

    getAuthHeaders() {
        if (!this.token) {
            return {};
        }
        return {
            'Authorization': `Bearer ${this.token}`
        };
    }

    async makeAuthenticatedRequest(url, options = {}) {
        const response = await fetch(url, {
            ...options,
            headers: {
                ...options.headers,
                ...this.getAuthHeaders()
            }
        });

        // Handle 401 errors by redirecting to login
        if (response.status === 401) {
            this.logout();
            window.location.href = '/login';
            throw new Error('Unauthorized');
        }

        return response;
    }

    getCurrentOrganization() {
        // For now, return the first organization (demo org)
        return this.organizations[0] || null;
    }

    logout() {
        // Clear all auth data
        localStorage.removeItem('clinicwire_token');
        localStorage.removeItem('clinicwire_user');
        localStorage.removeItem('clinicwire_organizations');
        localStorage.removeItem('clinicwire_demo_mode');
        
        // Reset instance variables
        this.token = null;
        this.user = null;
        this.organizations = [];
        this.isDemo = false;
    }

    requireAuth() {
        if (!this.isAuthenticated()) {
            window.location.href = '/login';
            return false;
        }
        return true;
    }
}

// Create global instance
window.authManager = new AuthManager();

// Helper function to add auth headers to existing code
function addAuthToHeaders(headers = {}) {
    return {
        ...headers,
        ...window.authManager.getAuthHeaders()
    };
}

// Helper to check if user is authenticated before making requests
function requireAuthForRequest() {
    if (!window.authManager.isAuthenticated()) {
        window.location.href = '/login';
        return false;
    }
    return true;
}

// Add demo mode indicator to page
function addDemoModeIndicator() {
    if (window.authManager.isDemo && window.authManager.isAuthenticated()) {
        const indicator = document.createElement('div');
        indicator.className = 'demo-mode-indicator';
        indicator.innerHTML = `
            <span>🔍 Demo Mode</span>
            <span class="org-name">${window.authManager.getCurrentOrganization()?.name || 'Demo Organization'}</span>
            <button onclick="handleLogout()" class="logout-btn">Logout</button>
        `;
        
        // Add styles
        const style = document.createElement('style');
        style.textContent = `
            .demo-mode-indicator {
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                background: #1e293b;
                border-bottom: 1px solid #334155;
                padding: 0.75rem 1rem;
                display: flex;
                align-items: center;
                gap: 1rem;
                z-index: 1000;
                font-size: 0.875rem;
                color: #94a3b8;
            }
            .demo-mode-indicator .org-name {
                font-weight: 600;
                color: #cbd5e1;
            }
            .demo-mode-indicator .logout-btn {
                margin-left: auto;
                background: #475569;
                color: #f1f5f9;
                border: none;
                padding: 0.375rem 0.75rem;
                border-radius: 0.375rem;
                cursor: pointer;
                font-size: 0.8125rem;
                transition: all 0.2s;
            }
            .demo-mode-indicator .logout-btn:hover {
                background: #64748b;
            }
            body.has-demo-indicator {
                padding-top: 52px;
            }
        `;
        document.head.appendChild(style);
        document.body.appendChild(indicator);
        document.body.classList.add('has-demo-indicator');
    }
}

// Global logout handler
function handleLogout() {
    if (confirm('Are you sure you want to logout?')) {
        window.authManager.logout();
        window.location.href = '/login';
    }
}

// Auto-initialize demo indicator when DOM loads
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', addDemoModeIndicator);
} else {
    addDemoModeIndicator();
}