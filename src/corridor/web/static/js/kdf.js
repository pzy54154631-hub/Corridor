// The access code's verifier (public mode). When a challenge carries `kdf`, the browser
// derives V = PBKDF2-HMAC-SHA256(code, salt, iterations), 256 bits as lower-case hex, and
// proves V with the same single-use HMAC as before; the server holds V and never the code.
// WebCrypto only runs in a secure context: without it nothing is derived and no proof is
// sent (the caller fails closed). The server's tool computes the same V with hashlib, and
// tests compare the two.

const ALGORITHM = "pbkdf2-sha256";
const SALT = /^[0-9a-f]{32}$/;
const MIN_ITERATIONS = 100000;
const MAX_ITERATIONS = 2000000;

// The challenge's `kdf` as checked parameters, or null when it is not one this page runs.
export function kdfParams(kdf) {
  if (!kdf || typeof kdf !== "object") return null;
  const { algorithm, iterations, salt } = kdf;
  if (algorithm !== ALGORITHM) return null;
  if (!Number.isInteger(iterations) || iterations < MIN_ITERATIONS || iterations > MAX_ITERATIONS) {
    return null;
  }
  if (typeof salt !== "string" || !SALT.test(salt)) return null;
  return { algorithm, iterations, salt };
}

function saltBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return bytes;
}

function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// The verifier of `code` for `kdf`, or null when the parameters or WebCrypto are missing.
export async function deriveVerifier(code, kdf) {
  const params = kdfParams(kdf);
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  if (!params || !subtle || typeof code !== "string") return null;
  const key = await subtle.importKey(
    "raw",
    new TextEncoder().encode(code),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: saltBytes(params.salt), iterations: params.iterations },
    key,
    256,
  );
  return hex(new Uint8Array(bits));
}
