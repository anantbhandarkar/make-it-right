# iOS storage, transport, and privacy declarations

Read at **Gate 5** (the storage plan) and **Gate 7** (the security review and the submission check).

**Verified 13 Aug 2026.** Store deadlines and the privacy-manifest mechanics live in `mir-mobile/references/store-compliance.md` — this file is the API-level detail.

---

## 1. Keychain accessibility classes

`kSecAttrAccessible` decides **when** the item is readable and **whether it can leave the device**. It is the single most consequential line in your Keychain code and it is almost always left at the default.

| Class | Readable when | In a backup? | Restores to another device? |
|---|---|---|---|
| `kSecAttrAccessibleWhenUnlocked` (default) | Device unlocked | Yes | **Yes** |
| `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` | Device unlocked | No | No |
| `kSecAttrAccessibleAfterFirstUnlock` | After the first unlock since boot | Yes | **Yes** |
| `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` | After the first unlock since boot | No | No |
| `kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly` | Device unlocked **and** a passcode is set; item is destroyed if the passcode is removed | No | No |
| `kSecAttrAccessibleAlways` / `…AlwaysThisDeviceOnly` | — | — | **Deprecated since iOS 12. Do not use.** |

Decision rule:

- A background task or a notification service extension must read it → `…AfterFirstUnlockThisDeviceOnly`.
- Foreground only → `…WhenUnlockedThisDeviceOnly`.
- Highest sensitivity, acceptable to lose if the user removes their passcode → `…WhenPasscodeSetThisDeviceOnly`.
- `kSecAttrSynchronizable = true` puts the item in iCloud Keychain and is **incompatible** with every `ThisDeviceOnly` class. Only set it for credentials the user genuinely expects on all their devices (a password, not a device-bound session token).

Other mechanics:

- **Items survive app deletion.** Wipe on first launch behind a `UserDefaults` flag (the flag *is* wiped). Neither half of that is a documented contract: Keychain persistence across uninstall is undocumented behavior, and a restore from backup returns the flag so the wipe is skipped. Use it as defence in depth behind short-lived, server-revocable tokens, and revalidate any recovered token before trusting it.
- `kSecAttrAccessGroup` shares items across your apps and extensions; the group must be in the entitlements of every one of them. Use this instead of a shared file for secrets.
- Binding to biometrics: create the item with `SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly, .biometryCurrentSet, &err)` and pass it as `kSecAttrAccessControl`. `.biometryCurrentSet` invalidates the item when the user enrols a new face or finger — you must handle `errSecItemNotFound` on that path and re-authenticate the user from scratch. `.biometryAny` does not invalidate and is therefore weaker.
- `SecItemAdd` returns `errSecDuplicateItem` rather than overwriting. Update-or-add, do not assume add.

---

## 2. What each store actually gives you

| Store | Encrypted at rest | In device backup | Shared with extensions | Survives reinstall |
|---|---|---|---|---|
| Keychain | Yes | Depends on class (above) | Only with an access group | **Yes — unless you wipe it** |
| `UserDefaults` / `@AppStorage` | No (plist in the container) | Yes | Yes, if in an App Group suite | No (restored from backup only) |
| App container files | Only per `FileProtectionType` | Yes, unless excluded | No | No |
| App Group container | Only per `FileProtectionType` | Yes | **Yes, to every app and extension in the group** | No |
| SwiftData / Core Data store | Only per `FileProtectionType` | Yes | Via App Group if placed there | No |

Set file protection explicitly; do not rely on the default:

```swift
try FileManager.default.setAttributes(
    [.protectionKey: FileProtectionType.complete], ofItemAtPath: url.path)
```

`.complete` = unreadable while the device is locked. `.completeUnlessOpen` if a background write must continue after lock. `.completeUntilFirstUserAuthentication` is the default for most files and is readable by any process after first unlock.

Exclude caches and derived data from backup with `URLResourceValues.isExcludedFromBackup = true`, both to keep the backup small and to keep derived copies of PII out of it.

---

## 3. App Transport Security

ATS is on by default. The keys that turn it off, in order of damage:

| Key | Effect |
|---|---|
| `NSAllowsArbitraryLoads` | Disables TLS requirements for the **entire app** |
| `NSAllowsArbitraryLoadsInWebContent` | Relaxes only `WKWebView` loads |
| `NSExceptionDomains` → `<host>` → `NSExceptionAllowsInsecureHTTPLoads` | Allows cleartext to one host |
| `NSExceptionDomains` → `<host>` → `NSExceptionMinimumTLSVersion` | Lowers the TLS floor for one host |

- Scope every exception to a host. Never ship `NSAllowsArbitraryLoads`.
- A debug-only exception written into the shared `Info.plist` ships. Verify against the **built** app: `plutil -p "$APP/Info.plist" | grep -i NSApp`.
- Certificate pinning goes in `URLSessionDelegate.urlSession(_:didReceive:completionHandler:)`. Pin to a public key, not a leaf certificate, and ship a backup pin — a pin with no rotation path bricks the app when the certificate rotates. A delegate that calls `completionHandler(.useCredential, URLCredential(trust: trust))` unconditionally disables validation entirely; that pattern shows up in copy-pasted "fix TLS errors" answers.

---

## 4. Pasteboard

- `UIPasteboard.general` is readable by every app on the device and, with Universal Clipboard, by the user's other Apple devices.
- Writing a secret:

```swift
UIPasteboard.general.setItems(
    [[UTType.plainText.identifier: otp]],
    options: [.localOnly: true, .expirationDate: Date().addingTimeInterval(60)])
```

- `.localOnly` keeps it off the user's other devices. `.expirationDate` clears it.
- Reading programmatically shows the system paste prompt (iOS 16+). To read without prompting, use `UIPasteControl` (the user's tap is the consent) or `UIPasteboard.detectPatterns(for:completionHandler:)`, which tells you *what kind* of content is present without revealing it.
- Named pasteboards created with `UIPasteboard(name:create:)` are no longer a private channel between your own apps — use an App Group instead.
- Exclude sensitive fields from the **app-switcher snapshot** by hiding them at `.inactive` (see `background-and-lifecycle.md`). That does **not** cover screen recording, mirroring, or AirPlay — those capture a fully `.active` app and never change the scene phase. Handle them separately: observe `UIScreen.capturedDidChangeNotification` and check `UIScreen.main.isCaptured`, then cover the sensitive view while capture is on. iOS has no API to block a user screenshot at all.
- Mark password fields with `.textContentType(.password)` so the system treats them correctly.

---

## 5. `PrivacyInfo.xcprivacy`

One file per app target; each SDK ships its own; Xcode merges them into the privacy report at archive time. **An SDK's manifest never covers your code.**

Keys: `NSPrivacyTracking`, `NSPrivacyTrackingDomains`, `NSPrivacyCollectedDataTypes`, `NSPrivacyAccessedAPITypes`.

Required-reason API categories that catch ordinary apps: `NSPrivacyAccessedAPICategoryUserDefaults`, file timestamp APIs, system boot time, disk space, and active keyboard information. Take the permitted reason codes from Apple's TN3183; an invented code is a rejection.

Rejection codes you will actually see:

| Code | Meaning |
|---|---|
| `ITMS-91053` | Missing API declaration — a required-reason API is used with no approved reason |
| `ITMS-91056` | The manifest exists but is malformed or invalid |
| `ITMS-91061` | Missing privacy manifest for a listed third-party SDK |
| `ITMS-91065` | Missing signature for a listed third-party SDK (a binary dependency) |

The manifest and the signature are two separate requirements with two separate codes — an SDK can ship a valid manifest and still fail on `ITMS-91065`.

Audit before every release:

```bash
# which dependencies ship a manifest
find . -name "PrivacyInfo.xcprivacy" -not -path "*/DerivedData/*"
# which required-reason APIs your own code touches
grep -rn "UserDefaults\|systemUptime\|\.creationDate\|volumeAvailableCapacity\|activeInputModes" Sources/
```

Then archive, open the privacy report, and diff it against the App Store Connect privacy answers. Re-run after **any** dependency change — that is the step that gets skipped.

---

## 6. Universal links vs. custom URL schemes

Custom schemes (`myapp://`) have no ownership model: any app may register the same scheme and iOS does not define which one receives the open. Anything carrying a token on a custom scheme can be intercepted.

Universal links setup:

1. Associated Domains entitlement: `applinks:example.com`.
2. `apple-app-site-association` served at `https://example.com/.well-known/apple-app-site-association`, `Content-Type: application/json`, **no redirects**, no `.json` extension, reachable without authentication.
3. `appIDs` entries are `TEAMID.bundle.id`. Use `components` with `?` / `#` / `exclude` to scope paths.
4. Handle with `.onOpenURL` (SwiftUI) or `application(_:continue:restorationHandler:)` (UIKit).
5. Debug with `applinks:example.com?mode=developer` plus the Developer settings toggle; the CDN caches the AASA file, so a fresh install is the honest test.

Whatever the transport, treat the link as untrusted input:

- It can arrive at cold start, before auth state is loaded. Route to a "resolving" state, not straight into an authenticated screen.
- Validate every parameter. An id in a link is a request, not permission — the server still does the object-level check.
- For OAuth use `ASWebAuthenticationSession` with PKCE. It uses the system browser, returns the callback to your app only, and does not need a custom scheme handler you control.

---

## 7. Jailbreak reality

Detection checks (`/Applications/Cydia.app`, writing outside the sandbox, `fork()` succeeding, `dyld` image names) are all readable and patchable by the tooling they are meant to detect. Frida can flip the return value of your `isJailbroken()` in one line. The same is true of certificate pinning and of any `LAContext.evaluatePolicy` result.

Use them to raise cost and to satisfy a compliance requirement. Do not use them as a control. Every decision that must hold — entitlement, price, quota, ownership — is made on the server, on a request the server authenticated.
