const BLOCKED_DOMAIN_SUFFIXES = [
  "localhost",
  "127.0.0.1",
  "0.0.0.0",
  "1password.com",
  "bitwarden.com",
  "lastpass.com",
  "dashlane.com",
  "accounts.google.com",
  "auth0.com",
  "okta.com",
  "myaccount.google.com",
  "meet.google.com",
  "zoom.us",
  "icloud.com",
  "gmail.com",
  "mail.google.com",
  "mail.yahoo.com",
  "proton.me",
  "protonmail.com",
  "fastmail.com",
  "login.microsoftonline.com",
  "outlook.live.com",
  "docs.google.com",
  "drive.google.com",
  "notion.com",
  "notion.so",
  "dropbox.com",
  "box.com",
  "bankofamerica.com",
  "capitalone.com",
  "chase.com",
  "citi.com",
  "americanexpress.com",
  "discover.com",
  "wellsfargo.com",
  "paypal.com",
  "venmo.com",
  "cash.app",
  "wise.com",
  "coinbase.com",
  "robinhood.com",
  "stripe.com",
  "web.whatsapp.com",
  "messages.google.com",
  "messenger.com",
  "web.telegram.org",
  "app.slack.com",
  "teams.microsoft.com",
  "discord.com",
  "chatgpt.com",
  "chat.openai.com",
  "claude.ai",
  "gemini.google.com",
  "gov",
  "healthcare.gov",
  "mychart.com",
  "mychart.org",
  "kp.org",
  "aetna.com",
  "uhc.com",
  "irs.gov",
  "usps.com",
  "talentplace.a16z.com"
];

const BLOCKED_URL_PATTERNS = [
  /\/login\b/i,
  /\/signin\b/i,
  /\/sign-in\b/i,
  /\/auth\b/i,
  /\/(?:oauth2?|oidc|saml)[^/?#]*(?:[/?#]|$)/i,
  /\/saml\b/i,
  /\/verify\b/i,
  /\/mfa\b/i,
  /\/2fa\b/i,
  /\/checkout\b/i,
  /\/billing\b/i,
  /\/invoice\b/i,
  /\/payment\b/i,
  /\/wallet\b/i,
  /\/transactions\b/i,
  /\/account\b/i,
  /\/settings\b/i,
  /\/password\b/i,
  /\/inbox\b/i,
  /\/messages\b/i,
  /\/direct\b/i,
  /\/dm\b/i,
  /\/patient\b/i,
  /\/medical\b/i,
  /\/mychart\b/i,
  /\/claim\b/i,
  /\/insurance\b/i,
  /\/appointment\b/i,
  /\/lab-results\b/i,
  /\/tax\b/i,
  /\/benefits\b/i,
  /\/candidate\b/i,
  /\/apply\b/i,
  /\/application\b/i,
  /\/dashboard\b/i,
  /\/questions\b/i,
  /\/eeo\b/i,
  /support\.google\.com\/meet\b/i,
  /mode=submit_apply/i,
  /uploadResume/i,
  /csrf=/i,
  /hashed=/i,
  /[?&#](?:code|state|token|access_token|refresh_token|id_token|authorization|session_token|samlresponse|assertion)=/i,
  /^chrome:\/\//i,
  /^chrome-extension:\/\//i,
  /^edge:\/\//i,
  /^brave:\/\//i,
  /^about:/i,
  /^file:\/\//i
];

export function getDomain(rawUrl) {
  try {
    return new URL(rawUrl).hostname.toLowerCase();
  } catch {
    return "";
  }
}

export function isBlockedUrl(rawUrl) {
  const domain = getDomain(rawUrl);

  if (!domain) {
    return {
      blocked: true,
      reason: "invalid-or-unsupported-url"
    };
  }

  const matchedDomain = BLOCKED_DOMAIN_SUFFIXES.find((blockedDomain) => {
    return domain === blockedDomain || domain.endsWith(`.${blockedDomain}`);
  });

  if (matchedDomain) {
    return {
      blocked: true,
      reason: `blocked-domain:${matchedDomain}`
    };
  }

  const matchedPattern = BLOCKED_URL_PATTERNS.find((pattern) => pattern.test(rawUrl));

  if (matchedPattern) {
    return {
      blocked: true,
      reason: `blocked-url-pattern:${matchedPattern.source}`
    };
  }

  return {
    blocked: false,
    reason: null
  };
}
