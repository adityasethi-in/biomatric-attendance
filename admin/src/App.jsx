import { useEffect, useRef, useState } from "react";
import {
  checkDuplicateStudentSamples,
  clearAdminSession,
  clearAttendance,
  clearScannerSession,
  confirmPasswordReset,
  configureDms,
  deleteAttendance,
  deleteStudent,
  disconnectDms,
  getActiveOrgSlug,
  getBillingPrice,
  getDmsOutbox,
  getDmsRoster,
  getDmsStatus,
  getOrganizations,
  getReport,
  getStudents,
  getSummary,
  loginAdmin,
  markAttendanceFromFrames,
  overrideScanner,
  registerOrganization,
  registerStudentSamples,
  requestPasswordReset,
  setActiveOrgSlug,
  setAdminSession,
  setScannerSession,
  warmupScanner,
} from "./api";

const ENROLLMENT_POSES = [
  "Look straight at the camera",
  "Turn your face slightly left",
  "Turn your face slightly right",
  "Lift your chin slightly",
  "Lower your chin slightly",
];

const SCAN_FRAME_COUNT = 3;
const SCAN_FRAME_DELAY_MS = 100;
const SCAN_COOLDOWN_MS = 1200;
const SCAN_SUCCESS_PAUSE_MS = 1800;

const DEFAULT_ORG_SLUG = "delight-model-school";
const DEFAULT_ADMIN_EMAIL = "admin@delightmodelschool.in";
const SCANNER_CLOSED_TEXT = "Scanner is available 7:00 AM to 11:00 AM. Admin can unlock for 30 minutes.";
const PASSWORD_FREE_SCANNER_TEXT = "No password required from 7:00 AM to 9:00 AM.";
const INDIAN_VOICE_KEYWORDS = [
  "india",
  "indian",
  "hindi",
  "neerja",
  "prabhat",
  "ravi",
  "heera",
  "swara",
  "madhur",
  "google hindi",
];
const emptyOrgRegistration = {
  organization_name: "",
  org_type: "school",
  contact_name: "",
  phone: "",
  email: "",
  seats: 100,
  billing_days: 30,
  payment_reference: "",
  admin_full_name: "",
  admin_username: "",
  admin_password: "",
};

function todayInputValue() {
  const now = new Date();
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
  return now.toISOString().slice(0, 10);
}

function formatFilterDateLabel(value) {
  if (!value) return "All dates";
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-IN", {
    weekday: "long",
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

export default function App() {
  const [portal, setPortal] = useState(window.location.pathname === "/admin" ? "admin" : "attendance");
  const [adminAuthenticated, setAdminAuthenticated] = useState(
    sessionStorage.getItem("admin_auth") === "true" && Boolean(sessionStorage.getItem("admin_token"))
  );
  const [scannerAuthenticated, setScannerAuthenticated] = useState(
    sessionStorage.getItem("scanner_auth") === "true" && Boolean(sessionStorage.getItem("scanner_token"))
  );
  const [scannerPasswordFree, setScannerPasswordFree] = useState(false);
  const [organizations, setOrganizations] = useState([]);
  const [selectedOrgSlug, setSelectedOrgSlug] = useState(
    sessionStorage.getItem("scanner_org_slug") || sessionStorage.getItem("admin_org_slug") || getActiveOrgSlug()
  );
  const [adminUsername, setAdminUsername] = useState(sessionStorage.getItem("admin_username") || DEFAULT_ADMIN_EMAIL);
  const [adminPassword, setAdminPassword] = useState("");
  const [adminOrgName, setAdminOrgName] = useState(sessionStorage.getItem("admin_org_name") || "Delight Model School");
  const [scannerUsername, setScannerUsername] = useState(sessionStorage.getItem("scanner_username") || DEFAULT_ADMIN_EMAIL);
  const [scannerPassword, setScannerPassword] = useState("");
  const [scannerOrgName, setScannerOrgName] = useState(sessionStorage.getItem("scanner_org_name") || "Delight Model School");
  const [scannerOverrideNeeded, setScannerOverrideNeeded] = useState(false);
  const [scannerOverridePassword, setScannerOverridePassword] = useState("");
  const [scannerOverrideUntil, setScannerOverrideUntil] = useState("");
  const [adminOverridePassword, setAdminOverridePassword] = useState("");
  const [adminOverrideUntil, setAdminOverrideUntil] = useState("");
  const [authMode, setAuthMode] = useState("login");
  const [resetEmail, setResetEmail] = useState(DEFAULT_ADMIN_EMAIL);
  const [resetToken, setResetToken] = useState("");
  const [resetNewPassword, setResetNewPassword] = useState("");
  const [resetRequested, setResetRequested] = useState(false);
  const [billingPrice, setBillingPrice] = useState({ price_per_user_per_day: 3, default_billing_days: 30 });
  const [orgRegistration, setOrgRegistration] = useState(emptyOrgRegistration);

  const [studentCode, setStudentCode] = useState("");
  const [fullName, setFullName] = useState("");
  const [personType, setPersonType] = useState("student");
  const [enrollmentSamples, setEnrollmentSamples] = useState([]);
  const [dmsLinkChoice, setDmsLinkChoice] = useState("");
  const [dmsLinkSearch, setDmsLinkSearch] = useState("");
  const [dmsLinkPickerOpen, setDmsLinkPickerOpen] = useState(false);

  const [dmsStatus, setDmsStatus] = useState({ linked: false });
  const [dmsRoster, setDmsRoster] = useState({ students: [], teachers: [] });
  const [dmsBaseUrl, setDmsBaseUrl] = useState("");
  const [dmsSecret, setDmsSecret] = useState("");
  const [dmsOutbox, setDmsOutbox] = useState([]);

  const [students, setStudents] = useState([]);
  const [report, setReport] = useState([]);
  const [attendanceDateFilter, setAttendanceDateFilter] = useState(todayInputValue());
  const [attendanceTypeFilter, setAttendanceTypeFilter] = useState("all");
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [registrationDecision, setRegistrationDecision] = useState(null);

  const [cameraOpen, setCameraOpen] = useState(false);
  const [openingCamera, setOpeningCamera] = useState(false);
  const [cameraLoading, setCameraLoading] = useState(false);
  const [flashState, setFlashState] = useState(null);
  const [scannerStatus, setScannerStatus] = useState("Camera starting...");
  const [lastScan, setLastScan] = useState(null);
  const [videoReady, setVideoReady] = useState(false);
  const [cameraDevices, setCameraDevices] = useState([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [streamVersion, setStreamVersion] = useState(0);

  const videoRef = useRef(null);
  const canvasRef = useRef(null);
  const streamRef = useRef(null);
  const dmsLinkPickerRef = useRef(null);
  const scanInFlightRef = useRef(false);
  const nextScanAllowedAtRef = useRef(0);
  const lastSpokenRef = useRef({ text: "", at: 0 });
  const speechVoicesRef = useRef([]);

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function getIndianSpeechVoice() {
    if (!("speechSynthesis" in window)) return null;
    const voices = speechVoicesRef.current.length ? speechVoicesRef.current : window.speechSynthesis.getVoices();
    if (!voices.length) return null;

    const scoredVoices = voices
      .map((voice) => {
        const lang = (voice.lang || "").toLowerCase();
        const name = (voice.name || "").toLowerCase();
        let score = 0;

        if (lang === "en-in") score += 100;
        if (lang === "hi-in") score += 90;
        if (lang.endsWith("-in")) score += 70;
        if (INDIAN_VOICE_KEYWORDS.some((keyword) => name.includes(keyword))) score += 55;
        if (name.includes("english") && (name.includes("india") || name.includes("indian"))) score += 25;
        if (voice.localService) score += 5;

        return { voice, score };
      })
      .filter(({ score }) => score > 0)
      .sort((a, b) => b.score - a.score);

    return scoredVoices[0]?.voice || null;
  }

  function speakAnnouncement(text, { minGapMs = 7000 } = {}) {
    const cleanText = String(text || "").trim();
    if (!cleanText || !("speechSynthesis" in window)) return;

    const now = Date.now();
    if (lastSpokenRef.current.text === cleanText && now - lastSpokenRef.current.at < minGapMs) {
      return;
    }

    try {
      window.speechSynthesis.cancel();
      const indianVoice = getIndianSpeechVoice();
      const utterance = new SpeechSynthesisUtterance(cleanText);
      if (indianVoice) utterance.voice = indianVoice;
      utterance.lang = indianVoice?.lang || "en-IN";
      utterance.rate = 0.9;
      utterance.pitch = 1.02;
      utterance.volume = 1;
      lastSpokenRef.current = { text: cleanText, at: now };
      window.speechSynthesis.speak(utterance);
    } catch {
      // Speech is a progressive enhancement; scanner should keep working silently if blocked.
    }
  }

  function getDmsPersonLabel(person) {
    if (!person) return "";
    return `${person.code} - ${person.fullName}${person.extra ? ` (${person.extra})` : ""}`;
  }

  function clearDmsLinkedDetails() {
    setStudentCode("");
    setFullName("");
    setPersonType("student");
  }

  function selectDmsPerson(person) {
    if (!person || person.linked) return;
    setDmsLinkChoice(person.value);
    setDmsLinkSearch(getDmsPersonLabel(person));
    setStudentCode(person.code || "");
    setFullName(person.fullName || "");
    setPersonType(person.kind === "teacher" ? "teacher" : "student");
    setDmsLinkPickerOpen(false);
  }

  function clearDmsLinkSelection() {
    setDmsLinkChoice("");
    setDmsLinkSearch("");
    clearDmsLinkedDetails();
    setDmsLinkPickerOpen(false);
  }

  async function loadAll() {
    const [s, r, summaryData, statusData] = await Promise.all([
      getStudents(),
      getReport({ date: attendanceDateFilter, personType: attendanceTypeFilter }),
      getSummary(),
      getDmsStatus().catch(() => ({ linked: false })),
    ]);
    setStudents(s.items || []);
    setReport(r.items || []);
    setSummary(summaryData);
    setDmsStatus(statusData);
    if (statusData.linked) {
      try {
        const [roster, outbox] = await Promise.all([
          getDmsRoster().catch(() => ({ students: [], teachers: [] })),
          getDmsOutbox().catch(() => ({ items: [] })),
        ]);
        setDmsRoster(roster);
        setDmsOutbox(outbox.items || []);
      } catch {
        // tolerated
      }
    } else {
      setDmsRoster({ students: [], teachers: [] });
      setDmsOutbox([]);
    }
  }

  async function onDmsConfigure(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      await configureDms({ baseUrl: dmsBaseUrl, webhookSecret: dmsSecret });
      setMessage("DMS link verified.");
      setDmsSecret("");
      await loadAll();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function onDmsDisconnect() {
    if (!window.confirm("Unlink DMS? Future scans will not push attendance.")) return;
    setLoading(true);
    setMessage("");
    try {
      await disconnectDms();
      setMessage("DMS unlinked.");
      await loadAll();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const resetTokenParam = params.get("reset_token");
    if (resetTokenParam) {
      setPortal("admin");
      setAuthMode("reset");
      setResetToken(resetTokenParam);
      setResetEmail(params.get("email") || DEFAULT_ADMIN_EMAIL);
      const orgParam = params.get("org");
      if (orgParam) {
        setSelectedOrgSlug(orgParam);
        setActiveOrgSlug(orgParam);
      }
    }

    Promise.all([getOrganizations(), getBillingPrice()])
      .then(([orgData, priceData]) => {
        const items = orgData.items || [];
        setOrganizations(items);
        setBillingPrice(priceData);
        if (!items.some((org) => org.slug === selectedOrgSlug)) {
          setSelectedOrgSlug(items[0]?.slug || DEFAULT_ORG_SLUG);
          setActiveOrgSlug(items[0]?.slug || DEFAULT_ORG_SLUG);
        }
      })
      .catch((err) => setMessage(err.message));
  }, []);

  useEffect(() => {
    setActiveOrgSlug(selectedOrgSlug);
    if (adminAuthenticated && portal === "admin") {
      loadAll().catch((err) => setMessage(err.message));
    }
  }, [selectedOrgSlug, adminAuthenticated, portal, attendanceDateFilter, attendanceTypeFilter]);

  useEffect(() => {
    if (!("speechSynthesis" in window)) return undefined;
    const synth = window.speechSynthesis;
    const loadVoices = () => {
      speechVoicesRef.current = synth.getVoices() || [];
    };

    loadVoices();
    if (typeof synth.addEventListener === "function") {
      synth.addEventListener("voiceschanged", loadVoices);
      return () => synth.removeEventListener("voiceschanged", loadVoices);
    }

    synth.onvoiceschanged = loadVoices;
    return () => {
      if (synth.onvoiceschanged === loadVoices) synth.onvoiceschanged = null;
    };
  }, []);

  function stopCameraStream() {
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
  }

  async function loadCameraDevices() {
    const devices = await navigator.mediaDevices.enumerateDevices();
    setCameraDevices(devices.filter((device) => device.kind === "videoinput"));
  }

  async function openCamera(deviceId = selectedDeviceId) {
    try {
      setMessage("");
      setOpeningCamera(true);
      setVideoReady(false);

      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("Camera needs HTTPS on mobile or localhost on laptop.");
      }

      stopCameraStream();
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: deviceId
            ? { deviceId: { exact: deviceId }, width: { ideal: 1280 }, height: { ideal: 720 } }
            : { facingMode: { ideal: "user" }, width: { ideal: 1280 }, height: { ideal: 720 } },
          audio: false,
        });
      } catch {
        stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      }

      streamRef.current = stream;
      const activeDevice = stream.getVideoTracks()[0]?.getSettings()?.deviceId || "";
      if (activeDevice) setSelectedDeviceId(activeDevice);
      setCameraOpen(true);
      setStreamVersion((version) => version + 1);
      await loadCameraDevices();
    } catch (err) {
      setMessage(err.message || "Camera permission denied or no camera found.");
    } finally {
      setOpeningCamera(false);
    }
  }

  function closeCamera() {
    stopCameraStream();
    setCameraOpen(false);
    setVideoReady(false);
    setScannerStatus("Camera closed.");
  }

  async function captureCurrentFrame() {
    if (!videoRef.current || !canvasRef.current) return null;
    if (!videoRef.current.videoWidth || !videoRef.current.videoHeight) {
      setMessage("Camera feed is not ready yet.");
      return null;
    }

    const video = videoRef.current;
    const canvas = canvasRef.current;
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.86));
  }

  async function captureScanFrames() {
    const frames = [];
    for (let index = 0; index < SCAN_FRAME_COUNT; index += 1) {
      const blob = await captureCurrentFrame();
      if (blob) frames.push(blob);
      if (index < SCAN_FRAME_COUNT - 1) {
        await sleep(SCAN_FRAME_DELAY_MS);
      }
    }
    return frames;
  }

  async function scanAttendanceFrame() {
    const now = Date.now();
    if (scanInFlightRef.current || now < nextScanAllowedAtRef.current) return;
    scanInFlightRef.current = true;
    setCameraLoading(true);
    setScannerStatus("Scanning...");
    let nextPauseMs = SCAN_COOLDOWN_MS;

    try {
      const frames = await captureScanFrames();
      if (frames.length < 2) return;
      const result = await markAttendanceFromFrames(frames);

      if (result.matched) {
        const confidence = result.confidence ?? Math.round((1 - result.distance) * 100);
        const type = result.person_type || "person";
        const text = result.already_marked
          ? `Attendance already marked for ${result.name}`
          : `Attendance marked for ${result.name}`;
        setLastScan({ text, confidence, type });
        setMessage("");
        speakAnnouncement(text);
        setScannerStatus("Recognized. Continuing scan...");
        setFlashState(result.already_marked ? "already" : "success");
        setTimeout(() => setFlashState(null), 1400);
        nextPauseMs = SCAN_SUCCESS_PAUSE_MS;
        if (adminAuthenticated) await loadAll();
      } else {
        setScannerStatus("No match yet. Keep face centered.");
        setFlashState("error");
        setTimeout(() => setFlashState(null), 900);
      }
    } catch (err) {
      if (err.status === 401) {
        clearScannerSession();
        setScannerAuthenticated(false);
        setScannerPasswordFree(false);
        closeCamera();
        setMessage(
          scannerPasswordFree
            ? "Password-free scanning ended at 9:00 AM. Please login to continue."
            : "Scanner login expired. Please login again."
        );
        return;
      }
      if (err.status === 503) {
        closeCamera();
        setScannerAuthenticated(false);
        setScannerOverrideNeeded(true);
        setScannerStatus(err.message);
        setMessage(err.message);
        return;
      }
      setScannerStatus(err.message);
      setFlashState("error");
      setTimeout(() => setFlashState(null), 1100);
    } finally {
      setCameraLoading(false);
      scanInFlightRef.current = false;
      nextScanAllowedAtRef.current = Date.now() + nextPauseMs;
    }
  }

  async function captureEnrollmentSample() {
    if (enrollmentSamples.length >= ENROLLMENT_POSES.length) return;
    setMessage("");
    const blob = await captureCurrentFrame();
    if (!blob) return;
    setEnrollmentSamples((samples) => [...samples, blob]);
  }

  async function completeRegistration(allowDuplicate = false, decision = registrationDecision) {
    setLoading(true);
    setMessage("");
    try {
      let dmsKind = "";
      let dmsId = "";
      if (dmsLinkChoice && dmsLinkChoice.includes(":")) {
        const [kind, id] = dmsLinkChoice.split(":");
        dmsKind = kind;
        dmsId = id;
      }
      const result = await registerStudentSamples({
        studentCode,
        fullName,
        personType,
        files: enrollmentSamples,
        allowDuplicate,
        dmsPersonKind: dmsKind,
        dmsPersonId: dmsId,
      });
      const registrationMessage = `${result.re_enrolled ? "Profile re-enrolled" : "Profile registered"} with ${result.sample_count} face samples.${result.dms_person_id ? " Linked to DMS." : ""}`;
      setMessage(registrationMessage);
      speakAnnouncement(`${result.re_enrolled ? "Profile re-enrolled" : "Profile registered"} for ${fullName}`, { minGapMs: 2500 });
      setStudentCode("");
      setFullName("");
      setPersonType("student");
      setEnrollmentSamples([]);
      setDmsLinkChoice("");
      setDmsLinkSearch("");
      setDmsLinkPickerOpen(false);
      await loadAll();
    } catch (err) {
      const duplicateDetail = err.data?.detail;
      if (err.status === 409 && duplicateDetail?.match) {
        setRegistrationDecision({
          type: "duplicate",
          match: duplicateDetail.match,
          threshold: duplicateDetail.threshold,
          serverRejected: true,
        });
        setMessage("");
        speakAnnouncement(`This face is already registered for ${duplicateDetail.match.full_name}`, { minGapMs: 2500 });
      } else {
        setMessage(err.message);
      }
    } finally {
      setLoading(false);
    }
  }

  async function onSubmit(e) {
    e.preventDefault();
    if (enrollmentSamples.length < ENROLLMENT_POSES.length) {
      setMessage("Capture all 5 face samples first.");
      return;
    }

    setLoading(true);
    setMessage("Checking if this face is already registered...");
    try {
      const check = await checkDuplicateStudentSamples({
        studentCode,
        files: enrollmentSamples,
      });

      setRegistrationDecision({
        type: check.duplicate ? "duplicate" : "new",
        match: check.match,
        nearestMatch: check.nearest_match,
        threshold: check.threshold,
        sampleCount: check.sample_count,
      });
      setMessage("");
      if (check.duplicate && check.match?.full_name) {
        speakAnnouncement(`This face is already registered for ${check.match.full_name}`, { minGapMs: 2500 });
      }
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function confirmRegistrationDecision() {
    const decision = registrationDecision;
    const allowDuplicate = decision?.type === "duplicate";
    setRegistrationDecision(null);
    await completeRegistration(allowDuplicate, decision);
  }

  async function onDeleteStudent(student) {
    const label = `${student.student_code} - ${student.full_name}`;
    if (!window.confirm(`Delete ${label}? This removes profile, face samples, and attendance history.`)) return;

    setLoading(true);
    setMessage("");
    try {
      await deleteStudent(student.id);
      setMessage(`${label} deleted.`);
      await loadAll();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function onDeleteAttendance(entry) {
    const label = `${entry.student_code} - ${entry.full_name}`;
    if (!window.confirm(`Delete attendance entry for ${label}?`)) return;

    setLoading(true);
    setMessage("");
    try {
      await deleteAttendance(entry.id);
      setMessage(`Attendance entry for ${label} deleted.`);
      await loadAll();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function onClearAttendance() {
    if (!window.confirm("Delete all attendance entries? Profiles and face samples will stay.")) return;

    setLoading(true);
    setMessage("");
    try {
      const result = await clearAttendance();
      setMessage(`${result.count || 0} attendance entries deleted.`);
      await loadAll();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  function resetEnrollmentSamples() {
    setEnrollmentSamples([]);
    setMessage("");
  }

  function matchConfidence(match) {
    if (!match) return 0;
    return match.confidence ?? Math.max(0, Math.min(100, Math.round((1 - match.distance) * 100)));
  }

  function navigate(path) {
    window.history.pushState({}, "", path);
    const nextPortal = path === "/admin" ? "admin" : "attendance";
    setPortal(nextPortal);
    setMessage("");
    if (nextPortal !== "attendance") closeCamera();
  }

  function onOrganizationChange(slug) {
    setSelectedOrgSlug(slug);
    setActiveOrgSlug(slug);
    const loggedAdminOrg = sessionStorage.getItem("admin_org_slug");
    const loggedScannerOrg = sessionStorage.getItem("scanner_org_slug");
    if (adminAuthenticated && loggedAdminOrg !== slug) {
      clearAdminSession();
      setAdminAuthenticated(false);
      setStudents([]);
      setReport([]);
      setSummary(null);
    }
    if (scannerAuthenticated && loggedScannerOrg !== slug) {
      clearScannerSession();
      setScannerAuthenticated(false);
      closeCamera();
      setScannerStatus("Login required before scanning.");
    }
  }

  async function onAdminLogin(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const result = await loginAdmin({
        organizationSlug: selectedOrgSlug,
        username: adminUsername,
        password: adminPassword,
      });
      setAdminSession(result);
      setAdminAuthenticated(true);
      setAdminOrgName(result.organization.name);
      setAdminPassword("");
      await loadAll();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function onPasswordResetRequest(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const result = await requestPasswordReset({
        organizationSlug: selectedOrgSlug,
        email: resetEmail,
      });
      setResetRequested(true);
      setMessage(result.message || "If this email is registered, a reset link has been sent.");
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function onPasswordResetConfirm(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const result = await confirmPasswordReset({
        organizationSlug: selectedOrgSlug,
        email: resetEmail,
        token: resetToken,
        newPassword: resetNewPassword,
      });
      setMessage(result.message || "Password updated. Please login with the new password.");
      setAdminUsername(resetEmail);
      setScannerUsername(resetEmail);
      setAdminPassword("");
      setScannerPassword("");
      setResetToken("");
      setResetNewPassword("");
      setResetRequested(false);
      setAuthMode("login");
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function onOrganizationRegister(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const result = await registerOrganization(orgRegistration);
      setMessage(`${result.organization.name} activated. Database: ${result.organization.database_name}`);
      setOrgRegistration(emptyOrgRegistration);
      const orgData = await getOrganizations();
      setOrganizations(orgData.items || []);
      onOrganizationChange(result.organization.slug);
      setAdminUsername(orgRegistration.admin_username);
      setAuthMode("login");
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function onScannerLogin(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const result = await loginAdmin({
        organizationSlug: selectedOrgSlug,
        username: scannerUsername,
        password: scannerPassword,
      });
      setScannerSession(result);
      setScannerOrgName(result.organization.name);
      setScannerStatus("Preparing scanner...");
      try {
        await warmupScanner();
      } catch (warmupError) {
        if (warmupError.status === 503) {
          setScannerOverrideNeeded(true);
          setScannerOverridePassword(scannerPassword);
          setScannerStatus(warmupError.message || SCANNER_CLOSED_TEXT);
          setMessage(warmupError.message || SCANNER_CLOSED_TEXT);
          return;
        }
        clearScannerSession();
        setScannerAuthenticated(false);
        setMessage(warmupError.message);
        return;
      }
      setScannerOverrideNeeded(false);
      setScannerPasswordFree(false);
      setScannerOverridePassword("");
      setScannerPassword("");
      setScannerAuthenticated(true);
      setScannerStatus("Login successful. Starting camera...");
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function unlockScannerForAttendance(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const result = await overrideScanner({
        organizationSlug: selectedOrgSlug,
        username: scannerUsername,
        password: scannerOverridePassword || scannerPassword,
      });
      if (result.token) {
        setScannerSession(result);
        setScannerOrgName(result.organization.name);
      }
      setScannerOverrideUntil(result.override_until || "");
      setScannerOverrideNeeded(false);
      setScannerPassword("");
      setScannerOverridePassword("");
      await warmupScanner();
      setScannerAuthenticated(true);
      setScannerStatus(`Scanner unlocked for ${result.override_minutes || 30} minutes.`);
      setMessage(`Scanner unlocked until ${new Date(result.override_until).toLocaleTimeString()}.`);
    } catch (err) {
      setMessage(err.message);
      setScannerStatus(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function unlockScannerForAdmin(e) {
    e.preventDefault();
    setLoading(true);
    setMessage("");
    try {
      const result = await overrideScanner({
        organizationSlug: selectedOrgSlug,
        username: adminUsername,
        password: adminOverridePassword,
      });
      if (result.token) setAdminSession(result);
      setAdminOverrideUntil(result.override_until || "");
      setAdminOverridePassword("");
      setMessage(`Face scanner unlocked until ${new Date(result.override_until).toLocaleTimeString()}.`);
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  function logoutScanner() {
    clearScannerSession();
    setScannerAuthenticated(false);
    setScannerPasswordFree(false);
    setScannerOverrideNeeded(false);
    setScannerOverridePassword("");
    closeCamera();
    setScannerStatus("Login required before scanning.");
  }

  function logoutAdmin() {
    clearAdminSession();
    setAdminAuthenticated(false);
    setStudents([]);
    setReport([]);
    setSummary(null);
    navigate("/");
  }

  useEffect(() => {
    return () => closeCamera();
  }, []);

  useEffect(() => {
    if (!cameraOpen || !videoRef.current || !streamRef.current) return;
    const video = videoRef.current;
    video.srcObject = streamRef.current;
    const onPlaying = () => {
      setVideoReady(true);
      if (portal === "attendance") setScannerStatus("Auto scanning...");
    };
    video.addEventListener("playing", onPlaying);
    video.onloadedmetadata = async () => {
      try {
        await video.play();
      } catch {
        setMessage("Camera connected, but browser blocked autoplay. Tap the camera preview once.");
      }
    };
    video.play().catch(() => {});
    return () => video.removeEventListener("playing", onPlaying);
  }, [cameraOpen, streamVersion, portal]);

  useEffect(() => {
    if (portal !== "attendance") return;
    if (!scannerAuthenticated) return;
    if (cameraOpen || openingCamera) return;
    openCamera();
  }, [portal, scannerAuthenticated]);

  useEffect(() => {
    if (portal !== "attendance" || scannerAuthenticated || !selectedOrgSlug) return undefined;
    let cancelled = false;

    async function startPasswordFreeScanner() {
      try {
        const result = await warmupScanner();
        if (cancelled || !result.password_free) return;
        setScannerOrgName(result.organization?.name || "Delight Model School");
        setScannerPasswordFree(true);
        setScannerOverrideNeeded(false);
        setScannerAuthenticated(true);
        setScannerStatus("Password-free scanning active until 9:00 AM.");
        setMessage("");
      } catch (err) {
        if (!cancelled && err.status !== 401 && err.status !== 503) {
          setMessage(err.message);
        }
      }
    }

    startPasswordFreeScanner();
    const intervalId = window.setInterval(startPasswordFreeScanner, 30000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [portal, scannerAuthenticated, selectedOrgSlug]);

  useEffect(() => {
    if (!scannerAuthenticated) return;
    if (portal !== "attendance" || !cameraOpen || !videoReady) return;
    setScannerStatus("Auto scanning...");
    const intervalId = window.setInterval(scanAttendanceFrame, 350);
    scanAttendanceFrame();
    return () => window.clearInterval(intervalId);
  }, [portal, cameraOpen, videoReady, streamVersion, scannerAuthenticated]);

  useEffect(() => {
    const onRoute = () => {
      const nextPortal = window.location.pathname === "/admin" ? "admin" : "attendance";
      setPortal(nextPortal);
      setMessage("");
      if (nextPortal !== "attendance") closeCamera();
    };
    window.addEventListener("popstate", onRoute);
    return () => window.removeEventListener("popstate", onRoute);
  }, []);

  useEffect(() => {
    const onPointerDown = (event) => {
      if (!dmsLinkPickerRef.current?.contains(event.target)) {
        setDmsLinkPickerOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, []);

  const dmsPersonOptions = [
    ...(dmsRoster.students || []).map((person) => ({
      ...person,
      kind: "student",
      kindLabel: "Student",
      value: `student:${person.person_id}`,
      fullName: person.full_name,
      linked: students.find((profile) => profile.dms_person_id === person.person_id),
    })),
    ...(dmsRoster.teachers || []).map((person) => ({
      ...person,
      kind: "teacher",
      kindLabel: "Teacher",
      value: `teacher:${person.person_id}`,
      fullName: person.full_name,
      linked: students.find((profile) => profile.dms_person_id === person.person_id),
    })),
  ];
  const selectedDmsPerson = dmsPersonOptions.find((person) => person.value === dmsLinkChoice);
  const dmsSearchQuery = dmsLinkSearch.trim().toLowerCase();
  const filteredDmsPeople = dmsPersonOptions.filter((person) => {
    if (!dmsSearchQuery) return true;
    return [person.code, person.fullName, person.extra, person.kindLabel]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(dmsSearchQuery);
  });

  const selectedOrganization = organizations.find((org) => org.slug === selectedOrgSlug);
  const advanceAmount =
    Number(orgRegistration.seats || 0) *
    Number(orgRegistration.billing_days || billingPrice.default_billing_days || 30) *
    Number(billingPrice.price_per_user_per_day || 3);
  const rosterStudents = dmsRoster.students || [];
  const rosterTeachers = dmsRoster.teachers || [];
  const countRosterLinked = (kind, roster) => {
    const linkedIds = new Set(
      students
        .filter((profile) => profile.dms_person_kind === kind && profile.dms_person_id)
        .map((profile) => String(profile.dms_person_id))
    );
    return roster.filter((person) => linkedIds.has(String(person.person_id))).length;
  };
  const studentLinked = countRosterLinked("student", rosterStudents);
  const teacherLinked = countRosterLinked("teacher", rosterTeachers);
  const studentProfiles = students.filter((profile) => profile.person_type === "student");
  const teacherProfiles = students.filter((profile) => profile.person_type === "teacher");
  const staffProfiles = students.filter((profile) => profile.person_type === "staff");
  const localStudentLinked = studentProfiles.filter((profile) => profile.dms_person_id).length;
  const localTeacherLinked = teacherProfiles.filter((profile) => profile.dms_person_id).length;
  const staffLinked = staffProfiles.filter((profile) => profile.dms_person_id).length;
  const hasStudentRoster = dmsStatus.linked && rosterStudents.length > 0;
  const hasTeacherRoster = dmsStatus.linked && rosterTeachers.length > 0;
  const buildCoverageCard = ({ title, subtitle, total, linked, tone }) => {
    const safeTotal = Number(total || 0);
    const safeLinked = Math.min(Number(linked || 0), safeTotal);
    const unlinked = Math.max(safeTotal - safeLinked, 0);
    const percent = safeTotal > 0 ? Math.round((safeLinked / safeTotal) * 100) : 0;
    return { title, subtitle, total: safeTotal, linked: safeLinked, unlinked, percent, tone };
  };
  const coverageCards = summary ? [
    buildCoverageCard({
      title: "Students",
      subtitle: hasStudentRoster ? "DMS roster coverage" : "Registered face profiles",
      total: hasStudentRoster ? rosterStudents.length : summary.students,
      linked: hasStudentRoster ? studentLinked : localStudentLinked,
      tone: "student",
    }),
    buildCoverageCard({
      title: "Teachers",
      subtitle: hasTeacherRoster ? "DMS roster coverage" : "Registered face profiles",
      total: hasTeacherRoster ? rosterTeachers.length : summary.teachers,
      linked: hasTeacherRoster ? teacherLinked : localTeacherLinked,
      tone: "teacher",
    }),
    buildCoverageCard({
      title: "Office / Staff",
      subtitle: "Local attendance profiles",
      total: summary.staff,
      linked: staffLinked,
      tone: "staff",
    }),
  ] : [];
  const attendanceTypeLabels = {
    all: "All people",
    student: "Students",
    teacher: "Teachers",
    staff: "Office / Staff",
  };
  const attendanceFilterSummary = `${formatFilterDateLabel(attendanceDateFilter)} | ${attendanceTypeLabels[attendanceTypeFilter] || "All people"}`;

  function updateOrgRegistration(field, value) {
    setOrgRegistration((current) => ({ ...current, [field]: value }));
  }

  const adminPanel = adminAuthenticated ? (
    <>
      <section className="card org-banner">
        <span>Logged in as</span>
        <strong>{adminOrgName}</strong>
        <small>{selectedOrganization?.is_free ? "Free internal account" : `Paid account | ${selectedOrganization?.seats || 0} users`}</small>
      </section>

      {summary && (
        <section className="card metrics-dashboard">
          <div className="metrics-hero">
            <div>
              <span className="metrics-kicker">Live attendance coverage</span>
              <h2>DMS linking dashboard</h2>
              <p>
                Track how many DMS people are connected to face profiles before daily attendance scans.
              </p>
            </div>
            <div className="metrics-score">
              <strong>{summary.total_people}</strong>
              <span>Face profiles</span>
            </div>
          </div>

          <div className="quick-metrics">
            <div>
              <strong>{summary.today_present}</strong>
              <span>Present today</span>
            </div>
            <div>
              <strong>{summary.total_samples}</strong>
              <span>Face samples</span>
            </div>
            <div>
              <strong>{summary.dms_pending || 0}</strong>
              <span>DMS pending</span>
            </div>
          </div>

          <div className="coverage-grid">
            {coverageCards.map((card) => (
              <article className={`coverage-card ${card.tone}`} key={card.title}>
                <div className="coverage-card-head">
                  <span className="coverage-mark">{card.title.slice(0, 2).toUpperCase()}</span>
                  <div>
                    <strong>{card.title}</strong>
                    <small>{card.subtitle}</small>
                  </div>
                </div>
                <div className="coverage-numbers">
                  <div><strong>{card.total}</strong><span>Total</span></div>
                  <div><strong>{card.linked}</strong><span>Linked</span></div>
                  <div><strong>{card.unlinked}</strong><span>Unlinked</span></div>
                </div>
                <div className="coverage-progress" aria-label={`${card.percent}% linked`}>
                  <span style={{ width: `${card.percent}%` }} />
                </div>
                <p>{card.percent}% linked</p>
              </article>
            ))}
          </div>
        </section>
      )}

      <section className="card">
        <div className="section-heading">
          <h2>Delight Model School link</h2>
          {dmsStatus.linked && <span className="status-pill ok">Connected</span>}
          {!dmsStatus.linked && <span className="status-pill off">Not linked</span>}
        </div>
        {dmsStatus.linked ? (
          <>
            <p className="hint-text">
              Attendance scans for profiles linked to a DMS person are mirrored to the school database automatically.
            </p>
            <div className="org-banner compact">
              <span>DMS endpoint</span>
              <strong>{dmsStatus.base_url}</strong>
              {dmsStatus.error && <small className="error-text">Last check: {dmsStatus.error}</small>}
            </div>
            {summary && (summary.dms_failing || 0) > 0 && (
          <p className="app-message">{summary.dms_failing} DMS sync item(s) need attention. Open DMS Outbox for details.</p>
            )}
            <button type="button" className="ghost-button" onClick={onDmsDisconnect} disabled={loading}>
              Unlink DMS
            </button>
          </>
        ) : (
          <form onSubmit={onDmsConfigure}>
            <label className="field-label">DMS API base URL</label>
            <input
              placeholder="https://dms.example.com/api/v1"
              value={dmsBaseUrl}
              onChange={(e) => setDmsBaseUrl(e.target.value)}
              required
            />
            <label className="field-label">Webhook shared secret</label>
            <input
              placeholder="Same value as BIOMATRIC_WEBHOOK_SECRET in DMS .env"
              value={dmsSecret}
              onChange={(e) => setDmsSecret(e.target.value)}
              type="password"
              required
            />
            <button disabled={loading} type="submit">{loading ? "Verifying..." : "Verify & link"}</button>
            <p className="hint-text">
              BIOMATRIC will sign every webhook with this secret. Configure it in DMS first as <code>BIOMATRIC_WEBHOOK_SECRET</code>.
            </p>
          </form>
        )}
      </section>

      <section className="card">
        <h2>Register Profile</h2>
        <p className="scanner-note">{SCANNER_CLOSED_TEXT}</p>
        <form className="override-form" onSubmit={unlockScannerForAdmin}>
          <input
            type="password"
            placeholder="Admin password to unlock face scanner"
            value={adminOverridePassword}
            onChange={(e) => setAdminOverridePassword(e.target.value)}
            required
          />
          <button disabled={loading} type="submit">{loading ? "Unlocking..." : "Unlock Face Scanner for 30 Min"}</button>
          {adminOverrideUntil && <p className="hint-text">Unlocked until {new Date(adminOverrideUntil).toLocaleTimeString()}.</p>}
        </form>
        <form onSubmit={onSubmit}>
          <input placeholder="ID / Roll number" value={studentCode} onChange={(e) => setStudentCode(e.target.value)} required />
          <input placeholder="Full name" value={fullName} onChange={(e) => setFullName(e.target.value)} required />
          <select value={personType} onChange={(e) => setPersonType(e.target.value)}>
            <option value="student">Student</option>
            <option value="staff">Staff</option>
            <option value="teacher">Teacher</option>
          </select>
          {!dmsStatus.linked && selectedOrganization?.is_free && (
            <div className="dms-empty-hint">
              <strong>No Delight Model School link yet</strong>
              <span>
                Scroll down to <em>Delight Model School link</em> and paste the DMS API URL +
                webhook secret. Once linked, this form will show a roster picker so you can map
                each face to a real student or teacher in DMS.
              </span>
            </div>
          )}
          {dmsStatus.linked && (
            <>
              <label className="field-label">Link to DMS person (optional)</label>
              <div className="dms-link-picker" ref={dmsLinkPickerRef}>
                <div className="dms-link-search-row">
                  <input
                    type="search"
                    placeholder="Search name, admission no, class..."
                    value={dmsLinkSearch}
                    autoComplete="off"
                    onFocus={() => setDmsLinkPickerOpen(true)}
                    onChange={(e) => {
                      const nextSearch = e.target.value;
                      setDmsLinkSearch(nextSearch);
                      setDmsLinkPickerOpen(true);
                      if (selectedDmsPerson && nextSearch !== getDmsPersonLabel(selectedDmsPerson)) {
                        setDmsLinkChoice("");
                        clearDmsLinkedDetails();
                      }
                    }}
                  />
                  {(dmsLinkChoice || dmsLinkSearch) && (
                    <button type="button" className="ghost-button dms-link-clear" onClick={clearDmsLinkSelection}>
                      Clear
                    </button>
                  )}
                </div>
                <p className="dms-link-helper">
                  {selectedDmsPerson
                    ? `Selected: ${getDmsPersonLabel(selectedDmsPerson)}`
                    : "Type or tap to search DMS roster. Already-linked people stay visible but cannot be selected again."}
                </p>
                {dmsLinkPickerOpen && (
                  <div className="dms-link-dropdown">
                    {filteredDmsPeople.length > 0 ? (
                      filteredDmsPeople.map((person) => (
                        <button
                          key={person.value}
                          type="button"
                          className={`dms-link-option${person.linked ? " is-linked" : ""}`}
                          disabled={!!person.linked}
                          onClick={() => selectDmsPerson(person)}
                        >
                          <span>
                            <strong>{person.fullName}</strong>
                            <small>
                              {person.kindLabel} - {person.code}{person.extra ? ` - ${person.extra}` : ""}
                            </small>
                          </span>
                          <em>{person.linked ? "Already linked" : "Select"}</em>
                        </button>
                      ))
                    ) : (
                      <div className="dms-link-empty">No DMS person found. Try another name, admission no, or class.</div>
                    )}
                  </div>
                )}
              </div>
            </>
          )}

          {!cameraOpen ? (
            <button type="button" disabled={openingCamera} onClick={() => openCamera()}>
              {openingCamera ? "Opening Camera..." : "Open Enrollment Camera"}
            </button>
          ) : (
            <>
              {cameraDevices.length > 1 && (
                <select value={selectedDeviceId} onChange={(e) => {
                  setSelectedDeviceId(e.target.value);
                  openCamera(e.target.value);
                }}>
                  {cameraDevices.map((device, index) => (
                    <option key={device.deviceId} value={device.deviceId}>
                      {device.label || `Camera ${index + 1}`}
                    </option>
                  ))}
                </select>
              )}
              <video ref={videoRef} autoPlay playsInline muted onClick={() => videoRef.current?.play()} className="camera-preview" />
              {!videoReady && <p>Connecting camera feed...</p>}
              <canvas ref={canvasRef} style={{ display: "none" }} />
              <div className="pose-panel">
                <strong>{enrollmentSamples.length < ENROLLMENT_POSES.length ? ENROLLMENT_POSES[enrollmentSamples.length] : "All samples captured"}</strong>
                <span>{enrollmentSamples.length} / {ENROLLMENT_POSES.length}</span>
              </div>
              <div className="sample-dots">
                {ENROLLMENT_POSES.map((pose, index) => (
                  <span key={pose} className={index < enrollmentSamples.length ? "dot done" : "dot"} title={pose} />
                ))}
              </div>
              <div className="split-actions">
                <button type="button" disabled={!videoReady || enrollmentSamples.length >= ENROLLMENT_POSES.length} onClick={captureEnrollmentSample}>
                  Capture Sample
                </button>
                <button type="button" onClick={resetEnrollmentSamples}>Retake</button>
              </div>
              <button type="button" onClick={closeCamera}>Close Camera</button>
            </>
          )}

          <button disabled={loading || enrollmentSamples.length < ENROLLMENT_POSES.length} type="submit">
            {loading ? "Registering..." : "Register With 5 Samples"}
          </button>
        </form>
      </section>

      <section className="card">
        <h2>Profiles</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>ID</th><th>Code</th><th>Name</th><th>Type</th><th>Samples</th><th>DMS</th><th>Action</th></tr>
            </thead>
            <tbody>
              {students.map((student) => (
                <tr key={student.id}>
                  <td>{student.id}</td>
                  <td>{student.student_code}</td>
                  <td>{student.full_name}</td>
                  <td>{student.person_type}</td>
                  <td>{student.sample_count}</td>
                  <td>
                    {student.dms_person_id
                      ? <span className="status-pill ok small">{student.dms_person_kind}</span>
                      : <span className="status-pill off small">—</span>}
                  </td>
                  <td><button className="danger-button" disabled={loading} onClick={() => onDeleteStudent(student)}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <div className="section-heading">
          <h2>Attendance</h2>
          <button className="danger-button" disabled={loading || report.length === 0} onClick={onClearAttendance}>Clear Entries</button>
        </div>
        <div className="attendance-filter-bar">
          <label className="filter-field">
            <span>Date / Day</span>
            <input type="date" value={attendanceDateFilter} onChange={(e) => setAttendanceDateFilter(e.target.value)} />
          </label>
          <label className="filter-field">
            <span>Person type</span>
            <select value={attendanceTypeFilter} onChange={(e) => setAttendanceTypeFilter(e.target.value)}>
              <option value="all">All people</option>
              <option value="student">Students</option>
              <option value="teacher">Teachers</option>
              <option value="staff">Office / Staff</option>
            </select>
          </label>
          <button type="button" className="ghost-button" onClick={() => setAttendanceDateFilter(todayInputValue())}>Today</button>
          <button type="button" className="ghost-button" onClick={() => setAttendanceDateFilter("")}>All Dates</button>
        </div>
        <p className="attendance-filter-summary">
          Showing {report.length} attendance entr{report.length === 1 ? "y" : "ies"} for {attendanceFilterSummary}
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>ID</th><th>Code</th><th>Name</th><th>Type</th><th>Status</th><th>Confidence</th><th>Time</th><th>Action</th></tr>
            </thead>
            <tbody>
              {report.length === 0 ? (
                <tr>
                  <td colSpan="8" className="empty-table">No attendance entries found for this filter.</td>
                </tr>
              ) : report.map((entry) => (
                <tr key={entry.id}>
                  <td>{entry.id}</td>
                  <td>{entry.student_code}</td>
                  <td>{entry.full_name}</td>
                  <td>{entry.person_type}</td>
                  <td>{entry.status}</td>
                  <td>{entry.confidence}</td>
                  <td>{new Date(entry.marked_at).toLocaleString()}</td>
                  <td><button className="danger-button" disabled={loading} onClick={() => onDeleteAttendance(entry)}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  ) : (
    <section className="card login-card">
      <div className="auth-tabs">
        <button type="button" className={authMode === "login" ? "" : "ghost-button"} onClick={() => setAuthMode("login")}>
          Login
        </button>
        <button type="button" className={authMode === "register" ? "" : "ghost-button"} onClick={() => setAuthMode("register")}>
          Register
        </button>
      </div>

      {authMode === "login" ? (
        <>
          <h2>Admin Login</h2>
          <form onSubmit={onAdminLogin}>
            <label className="field-label">Company / School</label>
            <select value={selectedOrgSlug} onChange={(e) => onOrganizationChange(e.target.value)}>
              {organizations.map((org) => (
                <option key={org.slug} value={org.slug}>
                  {org.name}{org.is_free ? " (Free)" : ""}
                </option>
              ))}
            </select>
            <input
              type="email"
              placeholder="Admin email"
              value={adminUsername}
              onChange={(e) => setAdminUsername(e.target.value.toLowerCase())}
              autoFocus
            />
            <input
              type="password"
              placeholder="Admin password"
              value={adminPassword}
              onChange={(e) => setAdminPassword(e.target.value)}
            />
            <button disabled={loading} type="submit">{loading ? "Logging in..." : "Login"}</button>
          </form>
          <button
            type="button"
            className="ghost-button full-width"
            onClick={() => {
              setResetEmail(adminUsername || DEFAULT_ADMIN_EMAIL);
              setAuthMode("reset");
            }}
          >
            Reset Password
          </button>
          <p className="hint-text">Delight Model School is free. Use the admin credentials configured on the server.</p>
        </>
      ) : authMode === "reset" ? (
        <>
          <h2>Reset Password</h2>
          {!resetRequested && !resetToken ? (
            <form onSubmit={onPasswordResetRequest}>
              <label className="field-label">Company / School</label>
              <select value={selectedOrgSlug} onChange={(e) => onOrganizationChange(e.target.value)}>
                {organizations.map((org) => (
                  <option key={org.slug} value={org.slug}>
                    {org.name}{org.is_free ? " (Free)" : ""}
                  </option>
                ))}
              </select>
              <input
                type="email"
                placeholder="Admin email"
                value={resetEmail}
                onChange={(e) => setResetEmail(e.target.value.toLowerCase())}
                required
              />
              <button disabled={loading} type="submit">{loading ? "Sending..." : "Send Reset Link"}</button>
            </form>
          ) : (
            <form onSubmit={onPasswordResetConfirm}>
              <input
                type="email"
                placeholder="Admin email"
                value={resetEmail}
                onChange={(e) => setResetEmail(e.target.value.toLowerCase())}
                required
              />
              <input
                placeholder="Reset token from email"
                value={resetToken}
                onChange={(e) => setResetToken(e.target.value)}
                required
              />
              <input
                type="password"
                placeholder="New password"
                value={resetNewPassword}
                onChange={(e) => setResetNewPassword(e.target.value)}
                required
              />
              <button disabled={loading} type="submit">{loading ? "Updating..." : "Update Password"}</button>
            </form>
          )}
          <button type="button" className="ghost-button full-width" onClick={() => setAuthMode("login")}>
            Back to Login
          </button>
          <p className="hint-text">Reset link will be sent to the registered admin email.</p>
        </>
      ) : (
        <>
          <h2>Register Organization</h2>
          <div className="price-card">
            <strong>Rs. {billingPrice.price_per_user_per_day || 3} per day per user</strong>
            <span>Advance payment required before admin activation.</span>
          </div>
          <form onSubmit={onOrganizationRegister}>
            <input placeholder="School / office / company name" value={orgRegistration.organization_name} onChange={(e) => updateOrgRegistration("organization_name", e.target.value)} required />
            <select value={orgRegistration.org_type} onChange={(e) => updateOrgRegistration("org_type", e.target.value)}>
              <option value="school">School</option>
              <option value="office">Office</option>
              <option value="coaching">Coaching</option>
              <option value="other">Other</option>
            </select>
            <input placeholder="Contact person name" value={orgRegistration.contact_name} onChange={(e) => updateOrgRegistration("contact_name", e.target.value)} required />
            <input placeholder="Phone number" value={orgRegistration.phone} onChange={(e) => updateOrgRegistration("phone", e.target.value)} required />
            <input type="email" placeholder="Email optional" value={orgRegistration.email} onChange={(e) => updateOrgRegistration("email", e.target.value)} />
            <input type="number" min="1" placeholder="How many users?" value={orgRegistration.seats} onChange={(e) => updateOrgRegistration("seats", e.target.value)} required />
            <input type="number" min="1" placeholder="Advance days" value={orgRegistration.billing_days} onChange={(e) => updateOrgRegistration("billing_days", e.target.value)} required />
            <div className="amount-box">
              <span>Advance payable</span>
              <strong>Rs. {Number.isFinite(advanceAmount) ? advanceAmount.toLocaleString("en-IN") : 0}</strong>
              <small>{orgRegistration.seats || 0} users x {orgRegistration.billing_days || 0} days x Rs. {billingPrice.price_per_user_per_day || 3}</small>
            </div>
            <input placeholder="Payment reference / UPI transaction ID" value={orgRegistration.payment_reference} onChange={(e) => updateOrgRegistration("payment_reference", e.target.value)} required />
            <input placeholder="Admin full name" value={orgRegistration.admin_full_name} onChange={(e) => updateOrgRegistration("admin_full_name", e.target.value)} required />
            <input type="email" placeholder="Create admin email" value={orgRegistration.admin_username} onChange={(e) => updateOrgRegistration("admin_username", e.target.value.toLowerCase())} required />
            <input type="password" placeholder="Create admin password" value={orgRegistration.admin_password} onChange={(e) => updateOrgRegistration("admin_password", e.target.value)} required />
            <button disabled={loading} type="submit">{loading ? "Activating..." : "Proceed & Create Admin"}</button>
          </form>
        </>
      )}
    </section>
  );

  const scannerPanel = (
    <section className="scanner-card">
      <div className="scanner-title">
        <h1>{scannerAuthenticated ? "Face the camera" : "Attendance Login"}</h1>
        <span>
          {scannerAuthenticated
            ? `${scannerOrgName} attendance marks automatically`
            : `Login first, then scanner will start. ${PASSWORD_FREE_SCANNER_TEXT}`}
        </span>
        {scannerAuthenticated && (
          <small className="scanner-note inline">
            {scannerPasswordFree ? "Password-free until 9:00 AM" : "Scanner ready"}
          </small>
        )}
      </div>

      {!scannerAuthenticated ? (
        <>
          <form onSubmit={onScannerLogin}>
            <label className="field-label">Company / School</label>
            <select value={selectedOrgSlug} onChange={(e) => onOrganizationChange(e.target.value)}>
              {organizations.map((org) => (
                <option key={org.slug} value={org.slug}>
                  {org.name}{org.is_free ? " (Free)" : ""}
                </option>
              ))}
            </select>
            <input
              type="email"
              placeholder="User email"
              value={scannerUsername}
              onChange={(e) => setScannerUsername(e.target.value.toLowerCase())}
              autoFocus
            />
            <input
              type="password"
              placeholder="User password"
              value={scannerPassword}
              onChange={(e) => setScannerPassword(e.target.value)}
            />
            <button disabled={loading} type="submit">
              {loading ? "Logging in..." : "Login & Start Scanner"}
            </button>
            <p className="hint-text">Use the scanner/admin credentials configured for this organization.</p>
          </form>
          {scannerOverrideNeeded && (
            <form className="override-form" onSubmit={unlockScannerForAttendance}>
              <div className="scanner-note">{SCANNER_CLOSED_TEXT}</div>
              <input
                type="password"
                placeholder="Admin password"
                value={scannerOverridePassword}
                onChange={(e) => setScannerOverridePassword(e.target.value)}
                required
              />
              <button disabled={loading} type="submit">{loading ? "Unlocking..." : "Unlock Scanner for 30 Min"}</button>
              {scannerOverrideUntil && <p className="hint-text">Unlocked until {new Date(scannerOverrideUntil).toLocaleTimeString()}.</p>}
            </form>
          )}
        </>
      ) : !cameraOpen ? (
        <>
          <div className="org-banner compact">
            <span>Scanner logged in</span>
            <strong>{scannerOrgName}</strong>
          </div>
          <button disabled={openingCamera} onClick={() => openCamera()}>
            {openingCamera ? "Opening Camera..." : "Start Camera"}
          </button>
          {!scannerPasswordFree && (
            <button className="ghost-button full-width" onClick={logoutScanner}>Logout Scanner</button>
          )}
        </>
      ) : (
        <>
          {cameraDevices.length > 1 && (
            <select value={selectedDeviceId} onChange={(e) => {
              setSelectedDeviceId(e.target.value);
              openCamera(e.target.value);
            }}>
              {cameraDevices.map((device, index) => (
                <option key={device.deviceId} value={device.deviceId}>
                  {device.label || `Camera ${index + 1}`}
                </option>
              ))}
            </select>
          )}
          <div className={`scan-stage ${flashState ? `scan-${flashState}` : ""}`}>
            <video ref={videoRef} autoPlay playsInline muted onClick={() => videoRef.current?.play()} className="camera-preview scanner-preview" />
            {flashState && (
              <div className={`recognition-overlay ${flashState}`}>
                <strong>
                  {flashState === "success" || flashState === "already"
                    ? lastScan?.text || "Attendance marked"
                    : "Not Recognized"}
                </strong>
                {(flashState === "success" || flashState === "already") && lastScan?.confidence && (
                  <span>{lastScan.type} | {lastScan.confidence}% match</span>
                )}
              </div>
            )}
          </div>
          <div className="scanner-status">
            <strong>{videoReady ? scannerStatus : "Connecting camera feed..."}</strong>
            {cameraLoading && <span>Processing frame</span>}
          </div>
          <canvas ref={canvasRef} style={{ display: "none" }} />
          <button className="ghost-button full-width" onClick={closeCamera}>Close Camera</button>
          <button className="ghost-button full-width" onClick={logoutScanner}>Logout Scanner</button>
        </>
      )}
    </section>
  );

  return (
    <div className="app-shell">
      <main className="phone-frame">
        <header className="app-header">
          <div>
            <strong>FacePass</strong>
            <span>{portal === "admin" ? "Admin" : "Attendance Scanner"}</span>
            <a className="header-credit" href="https://www.datacraftinsights.com/" target="_blank" rel="noreferrer">
              Built by DataCraftInsights.com
            </a>
          </div>
          {portal === "admin" ? (
            <div className="header-actions">
              <button onClick={() => navigate("/")}>Scan</button>
              {adminAuthenticated && <button className="ghost-button" onClick={logoutAdmin}>Logout</button>}
            </div>
          ) : (
            <button className="ghost-button" onClick={() => navigate("/admin")}>Admin Login</button>
          )}
        </header>

        {message && <p className="app-message">{message}</p>}
        {registrationDecision && (
          <div className="decision-backdrop" role="dialog" aria-modal="true">
            <div className={`decision-modal ${registrationDecision.type}`}>
              <span className="decision-kicker">Face duplicate check</span>
              {registrationDecision.type === "duplicate" ? (
                <>
                  <h2>Seems like this face is already registered</h2>
                  <div className="match-card">
                    <strong>{registrationDecision.match.full_name}</strong>
                    <span>ID: {registrationDecision.match.student_code}</span>
                    <span>Type: {registrationDecision.match.person_type}</span>
                    <span>Face match: {matchConfidence(registrationDecision.match)}%</span>
                  </div>
                  <p>
                    Same person alag ID/name se add ho sakta hai. Agar ye genuinely new profile hai tabhi add anyway karo.
                  </p>
                </>
              ) : (
                <>
                  <h2>No existing face match found</h2>
                  <p>
                    System ne database me same face nahi pakda. Please ID aur naam ek baar verify kar lo, phir profile add karo.
                  </p>
                  {registrationDecision.nearestMatch && (
                    <div className="match-card soft">
                      <strong>Nearest face: {registrationDecision.nearestMatch.full_name}</strong>
                      <span>ID: {registrationDecision.nearestMatch.student_code}</span>
                      <span>Similarity: {matchConfidence(registrationDecision.nearestMatch)}%</span>
                    </div>
                  )}
                </>
              )}
              <div className="decision-actions">
                <button type="button" className="ghost-button" onClick={() => setRegistrationDecision(null)}>
                  Review Again
                </button>
                <button type="button" disabled={loading} onClick={confirmRegistrationDecision}>
                  {registrationDecision.type === "duplicate" ? "Add Anyway" : "Yes, Add Profile"}
                </button>
              </div>
            </div>
          </div>
        )}
        {portal === "admin" ? adminPanel : scannerPanel}
        <footer className="brand-footer">
          <span>Built with care by</span>
            <a href="https://www.datacraftinsights.com/" target="_blank" rel="noreferrer">
              DataCraftInsights.com
            </a>
        </footer>
      </main>
    </div>
  );
}
