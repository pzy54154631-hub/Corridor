/*
 * HMAC-SHA256 for the live-model access field (S7 external review 3).
 *
 * The access code never leaves the browser: the field sends only
 * HMAC-SHA256(key = the code as UTF-8, message = "corridor-live-access-v1|" + nonce)
 * as lowercase hex, for a single-use nonce chosen by the server. Plain JavaScript with no
 * dependency. The field that uses it runs only in a secure context (HTTPS or a local
 * address): over an unencrypted connection this script itself could be replaced. The
 * server computes the same value with Python's hmac module (tests compare the two).
 */
(function (root) {
  "use strict";

  var K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
    0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
    0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
    0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
    0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
    0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
    0xc67178f2
  ];

  function rotr(x, n) {
    return (x >>> n) | (x << (32 - n));
  }

  function sha256(bytes) {
    var h = [
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
      0x5be0cd19
    ];
    var length = bytes.length;
    var padded = ((length + 9 + 63) >> 6) << 6;
    var message = new Uint8Array(padded);
    message.set(bytes);
    message[length] = 0x80;
    var view = new DataView(message.buffer);
    view.setUint32(padded - 8, Math.floor(length / 0x20000000));
    view.setUint32(padded - 4, (length << 3) >>> 0);
    var w = new Uint32Array(64);
    for (var offset = 0; offset < padded; offset += 64) {
      var i;
      for (i = 0; i < 16; i++) {
        w[i] = view.getUint32(offset + 4 * i);
      }
      for (i = 16; i < 64; i++) {
        var x = w[i - 15];
        var y = w[i - 2];
        var s0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3);
        var s1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      var a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], k = h[7];
      for (i = 0; i < 64; i++) {
        var t1 =
          (k + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
        var t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
        k = g;
        g = f;
        f = e;
        e = (d + t1) >>> 0;
        d = c;
        c = b;
        b = a;
        a = (t1 + t2) >>> 0;
      }
      h[0] = (h[0] + a) >>> 0;
      h[1] = (h[1] + b) >>> 0;
      h[2] = (h[2] + c) >>> 0;
      h[3] = (h[3] + d) >>> 0;
      h[4] = (h[4] + e) >>> 0;
      h[5] = (h[5] + f) >>> 0;
      h[6] = (h[6] + g) >>> 0;
      h[7] = (h[7] + k) >>> 0;
    }
    var out = new Uint8Array(32);
    var outView = new DataView(out.buffer);
    for (var j = 0; j < 8; j++) {
      outView.setUint32(4 * j, h[j]);
    }
    return out;
  }

  function hmac(key, message) {
    if (key.length > 64) {
      key = sha256(key);
    }
    var block = new Uint8Array(64);
    block.set(key);
    var inner = new Uint8Array(64 + message.length);
    var outer = new Uint8Array(64 + 32);
    for (var i = 0; i < 64; i++) {
      inner[i] = block[i] ^ 0x36;
      outer[i] = block[i] ^ 0x5c;
    }
    inner.set(message, 64);
    outer.set(sha256(inner), 64);
    return sha256(outer);
  }

  function hex(bytes) {
    var text = "";
    for (var i = 0; i < bytes.length; i++) {
      text += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    }
    return text;
  }

  var encoder = new TextEncoder();
  var api = {
    context: "corridor-live-access-v1|",
    proof: function (code, nonce) {
      return hex(hmac(encoder.encode(code), encoder.encode(api.context + nonce)));
    },
    hmacHex: function (key, message) {
      return hex(hmac(encoder.encode(key), encoder.encode(message)));
    }
  };
  root.CorridorHmac = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : this);
