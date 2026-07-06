(function () {
  const API = window.location.protocol === 'file:' ? 'http://localhost:5050' : '';
  const ACCESS_TOKEN_KEY = 'cs_access_token';
  const REFRESH_TOKEN_KEY = 'cs_refresh_token';
  const USER_KEY = 'cs_user';

  const linksByRole = {
    anonymous: [
      ['Search', '/search'],
      ['Topics', '/topics'],
      ['Login', '/login'],
      ['Register', '/register'],
    ],
    reader: [
      ['Search', '/search'],
      ['Dashboard', '/index.html'],
      ['Profile', '/profile'],
      ['Bookmarks', '/dashboard#bookmarks'],
      ['Collections', '/dashboard#collections'],
      ['Topics', '/topics'],
    ],
    publisher: [
      ['Search', '/search'],
      ['Dashboard', '/index.html'],
      ['Profile', '/profile'],
      ['Bookmarks', '/dashboard#bookmarks'],
      ['Collections', '/dashboard#collections'],
      ['Publisher Profile', '/publisher/profile'],
      ['Publisher Analytics', '/publisher/analytics'],
      ['Topics', '/topics'],
    ],
    reviewer: [
      ['Search', '/search'],
      ['Dashboard', '/index.html'],
      ['Profile', '/profile'],
      ['Moderation Queue', '/moderation'],
      ['Topics', '/topics'],
    ],
    admin: [
      ['Search', '/search'],
      ['Dashboard', '/index.html'],
      ['Profile', '/profile'],
      ['Users', '/users'],
      ['Applications', '/admin/publisher-applications'],
      ['Moderation', '/moderation'],
      ['Feed Management', '/index.html#admin'],
      ['Topics', '/topics'],
    ],
    super_admin: [
      ['Search', '/search'],
      ['Dashboard', '/index.html'],
      ['Profile', '/profile'],
      ['Users', '/users'],
      ['Applications', '/admin/publisher-applications'],
      ['Moderation', '/moderation'],
      ['Feed Management', '/index.html#admin'],
      ['Admin', '/index.html#admin'],
      ['System Controls', '/index.html#health'],
      ['Topics', '/topics'],
    ],
  };

  const roleLabels = {
    super_admin: 'Super Admin',
    admin: 'Admin',
    publisher: 'Publisher',
    reviewer: 'Reviewer',
    reader: 'Reader',
  };

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[c]));
  }

  function clearAuthState() {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  }

  function navIcon(name) {
    if (name === 'login') {
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l5-5-5-5"/><path d="M15 12H3"/></svg>';
    }
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>';
  }

  async function fetchCurrentUser() {
    const token = localStorage.getItem(ACCESS_TOKEN_KEY) || '';
    if (!token) return null;
    try {
      const res = await fetch(`${API}/api/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      if (data.user) localStorage.setItem(USER_KEY, JSON.stringify(data.user));
      return data.user || null;
    } catch {
      clearAuthState();
      return null;
    }
  }

  async function logoutUser() {
    const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY) || '';
    try {
      await fetch(`${API}/api/auth/logout`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(localStorage.getItem(ACCESS_TOKEN_KEY) ? { Authorization: `Bearer ${localStorage.getItem(ACCESS_TOKEN_KEY)}` } : {}),
        },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
    } finally {
      clearAuthState();
      window.location.href = '/login';
    }
  }

  function injectStyles() {
    if (document.getElementById('commonsource-role-nav-style')) return;
    const style = document.createElement('style');
    style.id = 'commonsource-role-nav-style';
    style.textContent = `
      .commonsource-role-badge {
        border: 1px solid rgba(255,255,255,0.28);
        border-radius: 999px;
        color: #fff;
        font-size: 0.68rem;
        font-weight: 800;
        letter-spacing: 0.06em;
        padding: 5px 9px;
        text-transform: uppercase;
      }
      .commonsource-nav-logout {
        font: inherit;
      }
      .commonsource-nav-icon {
        align-items: center;
        display: inline-flex;
        justify-content: center;
        min-height: 36px;
        min-width: 38px;
      }
      .commonsource-nav-icon svg {
        height: 18px;
        stroke-width: 2.3;
        width: 18px;
      }
    `;
    document.head.appendChild(style);
  }

  function renderRoleNav(container, user) {
    const role = user?.role || 'anonymous';
    const links = linksByRole[role] || linksByRole.reader;
    const items = links.map(([label, href]) => {
      if (label === 'Login') {
        return `<a class="commonsource-nav-icon" href="${escapeHtml(href)}" title="Login" aria-label="Login">${navIcon('login')}</a>`;
      }
      return `<a href="${escapeHtml(href)}">${escapeHtml(label)}</a>`;
    });
    if (user) {
      items.push(`<span class="commonsource-role-badge">[${escapeHtml(roleLabels[role] || role)}]</span>`);
      items.push(`<button class="commonsource-nav-logout commonsource-nav-icon" type="button" data-commonsource-logout title="Logout" aria-label="Logout">${navIcon('logout')}</button>`);
    }
    container.innerHTML = items.join('');
    container.querySelector('[data-commonsource-logout]')?.addEventListener('click', logoutUser);
  }

  async function initRoleNav(selector = '[data-role-nav]') {
    injectStyles();
    const containers = [...document.querySelectorAll(selector)];
    if (!containers.length) return null;
    const user = await fetchCurrentUser();
    containers.forEach(container => renderRoleNav(container, user));
    return user;
  }

  window.CommonSourceNav = { initRoleNav, logoutUser, fetchCurrentUser };
  document.addEventListener('DOMContentLoaded', () => initRoleNav());
}());
