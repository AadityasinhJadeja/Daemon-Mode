export const CAPTURE_CONSENT_VERSION = 1;
export const LOCAL_API_VERSION = 1;

export const OPTIONAL_PAGE_ORIGINS = ["http://*/*", "https://*/*"];

export function hasCurrentCaptureConsent(value) {
  return Number(value || 0) >= CAPTURE_CONSENT_VERSION;
}

export function evaluateServiceHealth(payload) {
  if (!payload || payload.ok !== true) {
    return {
      compatible: false,
      reason: "service-unavailable"
    };
  }

  const apiVersion = Number(payload.apiVersion);
  const minimum = Number(payload.minExtensionApiVersion);
  const maximum = Number(payload.maxExtensionApiVersion);

  if (![apiVersion, minimum, maximum].every(Number.isInteger)) {
    return {
      compatible: false,
      reason: "service-incompatible"
    };
  }

  const compatible = apiVersion === LOCAL_API_VERSION && minimum <= LOCAL_API_VERSION && maximum >= LOCAL_API_VERSION;
  return {
    compatible,
    reason: compatible ? null : "service-incompatible",
    apiVersion,
    serviceVersion: String(payload.serviceVersion || "")
  };
}

export function captureReadiness({ consentVersion, hostAccessGranted, serviceHealth }) {
  if (!hasCurrentCaptureConsent(consentVersion)) {
    return {
      ready: false,
      reason: "consent-required"
    };
  }

  if (!hostAccessGranted) {
    return {
      ready: false,
      reason: "host-access-withheld"
    };
  }

  const health = evaluateServiceHealth(serviceHealth);
  if (!health.compatible) {
    return {
      ready: false,
      reason: health.reason
    };
  }

  return {
    ready: true,
    reason: null
  };
}
