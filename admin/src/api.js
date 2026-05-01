const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const DEFAULT_ORG_SLUG = "delight-model-school";

export function getActiveOrgSlug() {
  return localStorage.getItem("active_org_slug") || DEFAULT_ORG_SLUG;
}

export function setActiveOrgSlug(slug) {
  localStorage.setItem("active_org_slug", slug || DEFAULT_ORG_SLUG);
}

export function setAdminSession({ organization, admin, token }) {
  setActiveOrgSlug(organization.slug);
  sessionStorage.setItem("admin_auth", "true");
  sessionStorage.setItem("admin_org_slug", organization.slug);
  sessionStorage.setItem("admin_org_name", organization.name);
  sessionStorage.setItem("admin_username", admin.username);
  sessionStorage.setItem("admin_token", token);
}

export function clearAdminSession() {
  sessionStorage.removeItem("admin_auth");
  sessionStorage.removeItem("admin_org_slug");
  sessionStorage.removeItem("admin_org_name");
  sessionStorage.removeItem("admin_username");
  sessionStorage.removeItem("admin_token");
}

export function setScannerSession({ organization, admin, token }) {
  setActiveOrgSlug(organization.slug);
  sessionStorage.setItem("scanner_auth", "true");
  sessionStorage.setItem("scanner_org_slug", organization.slug);
  sessionStorage.setItem("scanner_org_name", organization.name);
  sessionStorage.setItem("scanner_username", admin.username);
  sessionStorage.setItem("scanner_token", token);
}

export function clearScannerSession() {
  sessionStorage.removeItem("scanner_auth");
  sessionStorage.removeItem("scanner_org_slug");
  sessionStorage.removeItem("scanner_org_name");
  sessionStorage.removeItem("scanner_username");
  sessionStorage.removeItem("scanner_token");
}

function tenantHeaders() {
  return {
    "X-Org-Slug": getActiveOrgSlug(),
  };
}

function adminHeaders() {
  return {
    ...tenantHeaders(),
    "X-Admin-Username": sessionStorage.getItem("admin_username") || "",
    "X-Admin-Token": sessionStorage.getItem("admin_token") || "",
  };
}

function scannerHeaders() {
  return {
    "X-Org-Slug": sessionStorage.getItem("scanner_org_slug") || getActiveOrgSlug(),
    "X-User-Username": sessionStorage.getItem("scanner_username") || "",
    "X-User-Token": sessionStorage.getItem("scanner_token") || "",
  };
}

async function parseResponse(r, fallbackMessage) {
  const text = await r.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!r.ok) {
    const detail = data.detail;
    const error = new Error(
      typeof detail === "string" ? detail : detail?.message || fallbackMessage
    );
    error.status = r.status;
    error.data = data;
    throw error;
  }
  return data;
}

export async function getStudents() {
  const r = await fetch(`${API_BASE}/students`, { headers: adminHeaders() });
  return parseResponse(r, "Failed to load students");
}

export async function getReport() {
  const r = await fetch(`${API_BASE}/attendance/report`, { headers: adminHeaders() });
  return parseResponse(r, "Failed to load report");
}

export async function getSummary() {
  const r = await fetch(`${API_BASE}/admin/summary`, { headers: adminHeaders() });
  return parseResponse(r, "Failed to load summary");
}

export async function getOrganizations() {
  const r = await fetch(`${API_BASE}/organizations`);
  return parseResponse(r, "Failed to load organizations");
}

export async function getBillingPrice() {
  const r = await fetch(`${API_BASE}/billing/price`);
  return parseResponse(r, "Failed to load pricing");
}

export async function loginAdmin({ organizationSlug, username, password }) {
  const form = new FormData();
  form.append("organization_slug", organizationSlug);
  form.append("username", username);
  form.append("password", password);

  const r = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    body: form,
  });

  return parseResponse(r, "Login failed");
}

export async function registerOrganization(data) {
  const form = new FormData();
  Object.entries(data).forEach(([key, value]) => form.append(key, value));

  const r = await fetch(`${API_BASE}/organizations/register`, {
    method: "POST",
    body: form,
  });

  return parseResponse(r, "Organization registration failed");
}

export async function registerStudent({ studentCode, fullName, file }) {
  const form = new FormData();
  form.append("student_code", studentCode);
  form.append("full_name", fullName);
  form.append("person_type", "student");
  form.append("allow_duplicate", "false");
  form.append("image", file);

  const r = await fetch(`${API_BASE}/students/register`, {
    method: "POST",
    headers: adminHeaders(),
    body: form,
  });

  return parseResponse(r, "Registration failed");
}

export async function registerStudentSamples({ studentCode, fullName, personType, files, allowDuplicate = false, dmsPersonKind = "", dmsPersonId = "" }) {
  const form = new FormData();
  form.append("student_code", studentCode);
  form.append("full_name", fullName);
  form.append("person_type", personType);
  form.append("allow_duplicate", allowDuplicate ? "true" : "false");
  if (dmsPersonKind) form.append("dms_person_kind", dmsPersonKind);
  if (dmsPersonId) form.append("dms_person_id", dmsPersonId);
  files.forEach((file, index) => form.append("images", file, `sample-${index + 1}.jpg`));

  const r = await fetch(`${API_BASE}/students/register-samples`, {
    method: "POST",
    headers: adminHeaders(),
    body: form,
  });

  return parseResponse(r, "Registration failed");
}

export async function registerStudentClientSamples({
  studentCode,
  fullName,
  personType,
  embeddings,
  qualityScores = [],
  allowDuplicate = false,
  dmsPersonKind = "",
  dmsPersonId = "",
  model,
}) {
  const r = await fetch(`${API_BASE}/students/register-client-samples`, {
    method: "POST",
    headers: {
      ...adminHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      student_code: studentCode,
      full_name: fullName,
      person_type: personType,
      allow_duplicate: allowDuplicate,
      dms_person_kind: dmsPersonKind,
      dms_person_id: dmsPersonId,
      embeddings,
      quality_scores: qualityScores,
      model_name: model?.name,
      model_version: model?.version,
    }),
  });

  return parseResponse(r, "Client registration failed");
}

export async function checkDuplicateStudentSamples({ studentCode, files }) {
  const form = new FormData();
  form.append("student_code", studentCode);
  files.forEach((file, index) => form.append("images", file, `sample-${index + 1}.jpg`));

  const r = await fetch(`${API_BASE}/students/check-duplicate`, {
    method: "POST",
    headers: adminHeaders(),
    body: form,
  });

  return parseResponse(r, "Duplicate face check failed");
}

export async function checkDuplicateStudentClientSamples({ studentCode, embeddings, model }) {
  const r = await fetch(`${API_BASE}/students/check-duplicate-client`, {
    method: "POST",
    headers: {
      ...adminHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      student_code: studentCode,
      embeddings,
      model_name: model?.name,
      model_version: model?.version,
    }),
  });

  return parseResponse(r, "Client duplicate face check failed");
}
export async function deleteStudent(studentId) {
  const r = await fetch(`${API_BASE}/students/${studentId}`, {
    method: "DELETE",
    headers: adminHeaders(),
  });
  return parseResponse(r, "Delete failed");
}

export async function deleteAttendance(attendanceId) {
  const r = await fetch(`${API_BASE}/attendance/${attendanceId}`, {
    method: "DELETE",
    headers: adminHeaders(),
  });
  return parseResponse(r, "Delete attendance entry failed");
}

export async function clearAttendance() {
  const r = await fetch(`${API_BASE}/attendance`, {
    method: "DELETE",
    headers: adminHeaders(),
  });
  return parseResponse(r, "Clear attendance failed");
}

export async function getDmsStatus() {
  const r = await fetch(`${API_BASE}/dms/status`, { headers: adminHeaders() });
  return parseResponse(r, "Failed to load DMS status");
}

export async function configureDms({ baseUrl, webhookSecret }) {
  const form = new FormData();
  form.append("base_url", baseUrl);
  form.append("webhook_secret", webhookSecret);
  const r = await fetch(`${API_BASE}/dms/configure`, {
    method: "POST",
    headers: adminHeaders(),
    body: form,
  });
  return parseResponse(r, "DMS link failed");
}

export async function disconnectDms() {
  const r = await fetch(`${API_BASE}/dms/disconnect`, {
    method: "POST",
    headers: adminHeaders(),
  });
  return parseResponse(r, "DMS unlink failed");
}

export async function getDmsRoster() {
  const r = await fetch(`${API_BASE}/dms/roster`, { headers: adminHeaders() });
  return parseResponse(r, "Failed to load DMS roster");
}

export async function getDmsOutbox() {
  const r = await fetch(`${API_BASE}/dms/outbox`, { headers: adminHeaders() });
  return parseResponse(r, "Failed to load DMS outbox");
}

export async function markAttendanceFromFile(file) {
  const form = new FormData();
  form.append("image", file, "frame.jpg");

  const r = await fetch(`${API_BASE}/attendance/mark`, {
    method: "POST",
    headers: scannerHeaders(),
    body: form,
  });

  return parseResponse(r, "Attendance failed");
}

export async function markAttendanceWithEmbedding({ embedding, qualityScore, model }) {
  const r = await fetch(`${API_BASE}/attendance/mark-client`, {
    method: "POST",
    headers: {
      ...scannerHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      embedding,
      quality_score: qualityScore,
      model_name: model?.name,
      model_version: model?.version,
    }),
  });

  return parseResponse(r, "Client attendance failed");
}
