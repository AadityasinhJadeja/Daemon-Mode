export const PROTECTION_CHECK_UNAVAILABLE = {
  blocked: true,
  reason: "protection-check-unavailable"
};

export async function checkUserBlocklist(rawUrl, endpoint, fetchImpl = fetch) {
  try {
    const url = new URL(endpoint);
    url.searchParams.set("url", rawUrl);
    const response = await fetchImpl(url.toString());

    if (!response.ok) {
      return { ...PROTECTION_CHECK_UNAVAILABLE };
    }

    const payload = await response.json();
    if (payload?.ok !== true || typeof payload.blocked !== "boolean") {
      return { ...PROTECTION_CHECK_UNAVAILABLE };
    }

    if (payload.blocked) {
      return {
        blocked: true,
        reason: payload.reason ?? "user-blocked-domain"
      };
    }

    return {
      blocked: false,
      reason: null
    };
  } catch {
    return { ...PROTECTION_CHECK_UNAVAILABLE };
  }
}

async function checkDefaultProtectionPreference(rawUrl, reason, endpoint, fetchImpl) {
  try {
    const url = new URL(endpoint);
    url.searchParams.set("url", rawUrl);
    url.searchParams.set("reason", reason ?? "");
    const response = await fetchImpl(url.toString());

    if (!response.ok) {
      return { disabled: false };
    }

    const payload = await response.json();
    return {
      disabled: payload?.disabled === true
    };
  } catch {
    return { disabled: false };
  }
}

export async function resolveCaptureProtection(
  rawUrl,
  staticBlockResult,
  userEndpoint,
  defaultEndpoint,
  fetchImpl = fetch
) {
  // An owner's explicit block always wins, including when they have disabled a
  // matching starter protection. This must finish before page text is requested.
  const userBlockResult = await checkUserBlocklist(rawUrl, userEndpoint, fetchImpl);
  if (userBlockResult.blocked) {
    return userBlockResult;
  }

  if (!staticBlockResult?.blocked) {
    return userBlockResult;
  }

  const preference = await checkDefaultProtectionPreference(
    rawUrl,
    staticBlockResult.reason,
    defaultEndpoint,
    fetchImpl
  );
  if (preference.disabled) {
    return {
      blocked: false,
      reason: null
    };
  }

  return staticBlockResult;
}
