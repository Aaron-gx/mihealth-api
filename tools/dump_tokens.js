// Mi Health (com.mi.health) token + request dumper
// Attach to running app; dumps service tokens, plaintext params, decrypted responses.

function dumpObj(o, depth) {
    try { return "" + o; } catch (e) { return "<err>"; }
}

Java.perform(function () {
    send("[*] hooking TokenManagerImpl / CloudInterceptor / zi4");

    // ---- 1) service token capture ----
    try {
        var TM = Java.use("com.xiaomi.fitness.account.token.TokenManagerImpl");
        TM.getServiceToken.overload("java.lang.String", "boolean").implementation = function (sid, invalidate) {
            var t = this.getServiceToken(sid, invalidate);
            send("[TOKEN] getServiceToken sid=" + sid);
            if (t) {
                try {
                    send("[TOKEN]   serviceToken=" + t.getServiceToken());
                    send("[TOKEN]   security(ssecurity)=" + t.getSecurity());
                    send("[TOKEN]   cUserId=" + t.getCUserId());
                    send("[TOKEN]   userId=" + t.getUserId());
                    send("[TOKEN]   timeDiff=" + t.getTimeDiff());
                } catch (e) { send("[TOKEN] field err " + e); }
            } else send("[TOKEN]   result null");
            return t;
        };
        send("[+] TokenManagerImpl hooked");
    } catch (e) { send("[!] TM hook fail: " + e); }

    // ---- 2) SecretData per request + plaintext params ----
    try {
        var CI = Java.use("com.xiaomi.fitness.app.CloudInterceptor");
        // getEncryptedParams(method, path, map, secret, nonce, security) -> map
        CI.getEncryptedParams.implementation = function (m, path, map, secret, nonce, sec) {
            send("[REQ] " + m + " " + path + " params=" + (map ? map.toString() : "null"));
            var r = this.getEncryptedParams(m, path, map, secret, nonce, sec);
            send("[REQ]   -> encrypted=" + (r ? r.toString() : "null"));
            send("[REQ]   secret=" + (secret ? secret.toString() : "null") + " nonce=" + nonce);
            return r;
        };
        send("[+] CloudInterceptor.getEncryptedParams hooked");
    } catch (e) { send("[!] CI.getEncryptedParams fail: " + e); }

    try {
        var CI2 = Java.use("com.xiaomi.fitness.app.CloudInterceptor");
        CI2.decryptResponse.implementation = function (body, nonce, sec) {
            var plain = this.decryptResponse(body, nonce, sec);
            send("[RESP] decrypted=" + (plain && plain.length > 2000 ? plain.substring(0, 2000) + "...<trunc>" : plain));
            return plain;
        };
        send("[+] decryptResponse hooked");
    } catch (e) { send("[!] decryptResponse fail: " + e); }
});
