# .proxy

## NOTE⛔

This proxy list is collected from the list of proxies available on the Internet. They are only displayed in this repository for easy access. I am not responsible for proxies.


## Usage
Find proxies that are available in your country and use them in your applications.
1. Install the optional v2rayChecker client:
    ```console
    pip install --upgrade git+https://github.com/mheidari98/proxyUtil@main
    ```
2. Run v2rayChecker with 100 threads and check proxies:
    ```console
    v2rayChecker -v -T 100 --url 'https://raw.githubusercontent.com/mheidari98/.proxy/main/all'
    ```

## Source health

The hourly crawler fetches every source in `nodes.md` concurrently, under a download
deadline and size cap, and parses them with [proxyUtil](https://github.com/mheidari98/proxyUtil).
`nodes.md` shows the latest result per source; `source_status.json` keeps the history
(reachability, last valid data, last change, content hash, latency, failure streak).

- `healthy`: reachable and returned proxy configs recently
- `stale`: reachable, but its proxy set has not changed for seven days
- `failing`: currently unreachable, but not long enough to be considered dead
- `empty`: reachable, but returned no usable proxy configs
- `dead`: no valid proxy data for seven days


## What is published

Every hour the crawler merges all sources and then, before publishing:

1. drops configs that cannot work by their structure (bad port, private or reserved IP, bad
   host, empty credential),
2. merges configs that reach the same server the same way (differing only by name, parameter
   order, `fp`/`alpn`/`insecure`, ...),
3. drops configs whose server does not answer a TCP connect from the GitHub runner. A server
   that was already listed must fail three runs in a row; a new one is dropped at once. If the
   runner itself looks offline, nothing is dropped.

`all`, `vmess`, `vless`, `trojan` and `ss` are the filtered lists. **`raw`** is the same
list without step 3. A runner outside Iran cannot see everything a client inside Iran can
(measured 2026-10-30: 16 of 577 endpoints working from Iran did not answer from the runner),
so if the filtered list lacks something you need, use `raw`.

## Links 

  - **all**
    ```bash
    https://raw.githubusercontent.com/mheidari98/.proxy/main/all
    ```
  - **vmess**
    ```bash
    https://raw.githubusercontent.com/mheidari98/.proxy/main/vmess
    ```
  - **raw** (not reachability-filtered)
    ```bash
    https://raw.githubusercontent.com/mheidari98/.proxy/main/raw
    ```
  - **shadowsocks**
    ```bash
    https://raw.githubusercontent.com/mheidari98/.proxy/main/ss
    ```
