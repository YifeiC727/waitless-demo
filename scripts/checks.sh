#!/usr/bin/env bash
# 静态检查（第 3 步写，冻结：各施工线不改；tech §5.4、§7.1、§12；AC-17、AC-31、AC-100、AC-102、AC-103、AC-104）。
#
# 用法：scripts/checks.sh <trademarks|services|audio|hardcoded|secrets|friction [--until YYYY-MM-DD]|all [--until YYYY-MM-DD]>
# 退出码：0 通过；1 发现问题（打印命中的 文件:行）；2 用法错误或运行环境不对（不在仓库里、必需文件缺失）。
# 只读：不改文件、不出网、不读密钥。从任何目录调用都以本脚本所在仓库的根目录为准。
#
# 各检查的范围与规则（与 RUN/step3/tests/X/test-plan.md 第 2 节一致）：
#   trademarks  PRD AC-17 的路径（web server skills company evals replays README.md，不存在的跳过）里
#               不区分大小写搜 costco、bank of america（空格、连字符、下划线或无分隔都算）、整词 boa；
#               跳过 node_modules/、.venv/、venv/、__pycache__/、.pytest_cache/、.git/、.run/、*.lock、package-lock.json；二进制文件照扫。
#   services    server/waitless/**/*.py 里 boto3 客户端（.client( / .resource( / .create_client(）的服务名只能是
#               bedrock-runtime 或 polly，服务名不是字面量也算问题；server/pyproject.toml（非注释行）、
#               server/uv.lock（name = "…" 条目）、package.json、web/package.json 里没有 twilio、Amazon Connect、云存储 SDK。
#   audio       git ls-files（已跟踪或已暂存的文件）里没有音频扩展名的文件。
#   hardcoded   server/waitless 与 web/js 下的文件：URL（任何 scheme://）、localhost、127.0.0.1、0.0.0.0、"::1"/[::1]、
#               IPv4 字面量、port= 后的数字、host= 后的带点主机名、域名:端口、冒号后的 4–5 位数字。
#               白名单：行尾 `# allow-literal` 或 `// allow-literal`；源码引用「文件.py:行号」；切片 [..:N]；
#               协议常量 8000、16000、24000、48000（采样率）、4401（WebSocket 关闭码）、DTMF 列频率 1209、1336、1477、1633
#               （160、20、640 与 DTMF 行频率 697/770/852/941 位数不足 4 位，规则本身不会命中）；端口语境（port=、域名:）不享受白名单。
#   secrets     git grep（已跟踪文件）：AKIA/ASIA 开头的 20 位访问密钥、secret…key 赋值后的 40 位密钥、
#               -----BEGIN … PRIVATE KEY 块、Bearer 后跟 20 位以上的字面量令牌。只打印 文件:行 和类别，不打印命中的值。
#   friction    根目录 FRICTION-LOG.md 存在；2026-10-14 … 2026-10-19 每天至少一条条目。条目是：以该日期开头且后面有文字的一行
#               （可带 - * + 1. | ** 前缀），或日期标题（# … 2026-10-14）之下至少一行非空正文。--until DATE 只检查不晚于 DATE 的日期。
set -euo pipefail
# 按字节处理：二进制或非 UTF-8 内容不能让 sed/sort/awk 报错后被当成「没有命中」。
export LC_ALL=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FRICTION_DATES=(2026-10-14 2026-10-15 2026-10-16 2026-10-17 2026-10-18 2026-10-19)
TRADEMARK_PATHS=(web server skills company evals replays README.md)
SCAN_EXCLUDE_DIRS=(node_modules .venv venv __pycache__ .pytest_cache .git .run)
HARDCODED_DIRS=(server/waitless web/js)
ALLOWED_BOTO3_SERVICES="bedrock-runtime polly"
AUDIO_EXT_RE='\.(wav|mp3|pcm|ulaw|mulaw|m4a|ogg|webm|flac|aac|aif|aiff|opus|amr|wma|caf|raw)$'
DEP_FORBIDDEN_RE='twilio|amazon-connect|(mypy|types)-(boto3|aiobotocore)-(connect|s3)|boto3-stubs\[[^]]*(connect|s3)|google-cloud-storage|azure-storage|s3fs|gcsfs|adlfs|minio|aioboto3|smart[-_]open|cloudpathlib|firebase-admin|dropbox|oss2'
NPM_FORBIDDEN_RE='"(twilio|amazon-connect[^"]*|@aws-sdk/client-(s3|connect)|@google-cloud/storage|@azure/storage-[a-z-]+|aws-sdk|firebase|firebase-admin)"[[:space:]]*:'

# grep 系列命令的包装：命中或未命中都返回 0（输出就是命中），出错返回 2。
grep_hits() {
  local rc=0
  "$@" || rc=$?
  if [ "$rc" -gt 1 ]; then return 2; fi
  return 0
}

need_git_tree() {
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "checks.sh $1: $ROOT is not a git work tree" >&2
    return 2
  fi
}

report() {
  # report <check> <hits>：有命中就打印并返回 1。
  local name="$1" hits="$2"
  if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    echo "checks.sh $name: FAILED" >&2
    return 1
  fi
  echo "checks.sh $name: ok" >&2
  return 0
}

# ---------------------------------------------------------------------------
# AC-17：新系统路径里没有第三方商标（原型旧文件不在范围内，K19、N7）。
# ---------------------------------------------------------------------------
check_trademarks() {
  if [ "$#" -ne 0 ]; then usage; return 2; fi
  local paths=() p d
  for p in "${TRADEMARK_PATHS[@]}"; do
    if [ -e "$p" ]; then paths+=("$p"); fi
  done
  if [ "${#paths[@]}" -eq 0 ]; then
    echo "checks.sh trademarks: none of ${TRADEMARK_PATHS[*]} exists under $ROOT" >&2
    return 2
  fi
  local opts=(-r -n -i -a)  # -a：二进制文件也按文本扫（PRD 的 grep 对二进制命中同样有输出；GNU grep 3.5 起该提示改写到 stderr）
  for d in "${SCAN_EXCLUDE_DIRS[@]}"; do opts+=("--exclude-dir=$d"); done
  opts+=("--exclude=*.lock" "--exclude=package-lock.json")
  local a b
  a="$(grep_hits grep "${opts[@]}" -E -e 'costco|bank[[:space:]_-]*of[[:space:]_-]*america' -- "${paths[@]}")" || return 2
  b="$(grep_hits grep "${opts[@]}" -w -E -e 'boa' -- "${paths[@]}")" || return 2
  local hits
  hits="$(printf '%s\n%s' "$a" "$b" | sed '/^$/d' | sort -u)" || return 2
  report trademarks "$hits"
}

# ---------------------------------------------------------------------------
# AC-31：boto3 客户端只有 bedrock-runtime 和 polly；依赖里没有 twilio、Connect 或云存储 SDK。
# ---------------------------------------------------------------------------
# awk 程序放在函数里的 here-doc 中（不放进 $(...)，避免 bash 3.2 在命令替换里误判引号）。
awk_services() {
  cat <<'AWK'
BEGIN { n = split(allowed, ok, " ") }
function allowed_name(s,   i) { for (i = 1; i <= n; i++) if (s == ok[i]) return 1; return 0 }
{
  line = $0
  if (line ~ /^[ \t]*#/) next
  if (line !~ /boto3|[Ss]ession/) next
  rest = line
  found = 0
  while (match(rest, /\.[ \t]*(client|resource|create_client)[ \t]*\(/)) {
    found = 1
    rest = substr(rest, RSTART + RLENGTH)
    arg = rest
    sub(/^[ \t]*/, "", arg)
    sub(/^service_name[ \t]*=[ \t]*/, "", arg)
    q = substr(arg, 1, 1)
    svc = ""
    if (q == "\"" || q == "'") {
      body = substr(arg, 2)
      k = index(body, q)
      if (k > 0) svc = substr(body, 1, k - 1)
    }
    if (svc == "") print FILENAME ":" FNR ": boto3 client with a non-literal service name"
    else if (!allowed_name(svc)) print FILENAME ":" FNR ": boto3 client for service '" svc "'"
  }
}
AWK
}

check_services() {
  if [ "$#" -ne 0 ]; then usage; return 2; fi
  if [ ! -d server/waitless ]; then echo "checks.sh services: server/waitless not found under $ROOT" >&2; return 2; fi
  if [ ! -f server/pyproject.toml ]; then echo "checks.sh services: server/pyproject.toml not found under $ROOT" >&2; return 2; fi
  local code deps lock npm f
  code="$(find server/waitless -type f -name '*.py' -not -path '*/__pycache__/*' -print0 \
          | xargs -0 -r awk -v allowed="$ALLOWED_BOTO3_SERVICES" "$(awk_services)")" || return 2
  deps="$(grep_hits grep -n -H -i -E -e "$DEP_FORBIDDEN_RE" server/pyproject.toml)" || return 2
  deps="$(printf '%s\n' "$deps" | grep -v -E '^[^:]*:[0-9]+:[[:space:]]*#' | sed '/^$/d' || true)"
  lock=""
  if [ -f server/uv.lock ]; then
    lock="$(grep_hits grep -n -H -i -E -e "name = \"($DEP_FORBIDDEN_RE)\"" server/uv.lock)" || return 2
  fi
  npm=""
  for f in package.json web/package.json; do
    if [ -f "$f" ]; then
      npm+="$(grep_hits grep -n -H -i -E -e "$NPM_FORBIDDEN_RE" "$f")" || return 2
    fi
  done
  local hits
  hits="$(printf '%s\n%s\n%s\n%s' "$code" "$deps" "$lock" "$npm" | sed '/^$/d')" || return 2
  report services "$hits"
}

# ---------------------------------------------------------------------------
# AC-100：仓库里没有音频文件。
# ---------------------------------------------------------------------------
check_audio() {
  if [ "$#" -ne 0 ]; then usage; return 2; fi
  need_git_tree audio || return 2
  local files hits
  files="$(git ls-files -z | tr '\0' '\n')" || { echo "checks.sh audio: git ls-files failed" >&2; return 2; }
  hits="$(printf '%s\n' "$files" | grep -i -E -e "$AUDIO_EXT_RE" || true)"
  report audio "$hits"
}

# ---------------------------------------------------------------------------
# AC-102：server/waitless 与 web/js 没有端口、主机、URL 字面量（白名单外为空）。
# ---------------------------------------------------------------------------
# awk 程序放在函数里的 here-doc 中（不放进 $(...)，避免 bash 3.2 在命令替换里误判引号）。
awk_hardcoded() {
  cat <<'AWK'
function flag(kind) { print FILENAME ":" FNR ": [" kind "] " $0 }
{
  raw = $0
  if (raw ~ "(#|//)[ \t]*allow-literal[ \t]*$") next
  low = tolower(raw)
  if (low ~ /localhost/) { flag("localhost"); next }
  if (raw ~ /127\.0\.0\.1/) { flag("loopback address"); next }
  if (raw ~ /0\.0\.0\.0/) { flag("wildcard address"); next }
  if (raw ~ /["']::1["']/ || raw ~ /["']\[::1\]/ || raw ~ /\[::1\]:[0-9]/) { flag("ipv6 loopback"); next }
  if (raw ~ /(^|[^0-9.])[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?([^0-9.]|$)/) { flag("ipv4 literal"); next }
  if (raw ~ "[A-Za-z][A-Za-z0-9+.-]*://") { flag("url literal"); next }
  if (low ~ /(^|[^a-z0-9])([a-z0-9]*_)?port["']?[ \t]*(=|:)[ \t]*["']?[0-9]/) { flag("port literal"); next }
  if (low ~ /(^|[^a-z0-9])([a-z0-9]*_)?(host|hostname)["']?[ \t]*(=|:)[ \t]*["'][a-z0-9-]+(\.[a-z0-9-]+)+["']/) { flag("host literal"); next }
  s = raw
  gsub("[A-Za-z0-9_./-]+[.](py|pyi|mjs|cjs|js|ts|json|yaml|yml|toml|md|html|txt|cfg|ini|sh):[0-9]+(-[0-9]+)?", "", s)
  if (s ~ /[A-Za-z0-9-]+\.[A-Za-z][A-Za-z]+:[0-9][0-9]/) { flag("host:port literal"); next }
  gsub(/\[[^]]*:[0-9]+\]/, "", s)
  gsub(/:(8000|16000|24000|48000|4401|1209|1336|1477|1633)([^0-9]|$)/, " ", s)
  if (s ~ /:[0-9][0-9][0-9][0-9][0-9]?([^0-9]|$)/) { flag("port-like number"); next }
}
AWK
}

check_hardcoded() {
  if [ "$#" -ne 0 ]; then usage; return 2; fi
  local dirs=() d hits
  for d in "${HARDCODED_DIRS[@]}"; do
    if [ -d "$d" ]; then dirs+=("$d"); fi
  done
  if [ "${#dirs[@]}" -eq 0 ]; then
    echo "checks.sh hardcoded: none of ${HARDCODED_DIRS[*]} exists under $ROOT" >&2
    return 2
  fi
  hits="$(find "${dirs[@]}" -type f -not -path '*/__pycache__/*' -not -path '*/node_modules/*' \
            -not -name '*.pyc' -not -name '.DS_Store' -print0 \
          | xargs -0 -r awk "$(awk_hardcoded)")" || return 2
  report hardcoded "$hits"
}

# ---------------------------------------------------------------------------
# AC-103：全仓库（已跟踪文件）没有密钥。只打印 文件:行 与类别，不打印值。
# ---------------------------------------------------------------------------
secret_hits() {
  # secret_hits <类别> <git grep 参数…>
  local kind="$1"; shift
  local out
  out="$(grep_hits git grep -n -I "$@")" || return 2
  if [ -n "$out" ]; then
    printf '%s\n' "$out" | cut -d: -f1,2 | sed "s/\$/: $kind/"
  fi
}

check_secrets() {
  if [ "$#" -ne 0 ]; then usage; return 2; fi
  need_git_tree secrets || return 2
  local a b c d
  a="$(secret_hits aws-access-key-id -E -e '(^|[^A-Za-z0-9])(AKIA|ASIA)[0-9A-Z]{16}([^A-Za-z0-9]|$)')" || return 2
  b="$(secret_hits aws-secret-access-key -i -E -e "secret[_-]?(access[_-]?)?key[\"']?[[:space:]]*[:=>]+[[:space:]]*[\"']?[A-Za-z0-9/+]{40}([^A-Za-z0-9/+=]|\$)")" || return 2
  c="$(secret_hits private-key-block -E -e '-----BEGIN[ A-Z0-9]*PRIVATE KEY')" || return 2
  d="$(secret_hits bearer-token -i -E -e 'bearer[[:space:]]+[A-Za-z0-9._~+/-]{20,}')" || return 2
  local hits
  hits="$(printf '%s\n%s\n%s\n%s' "$a" "$b" "$c" "$d" | sed '/^$/d' | sort -u)" || return 2
  report secrets "$hits"
}

# ---------------------------------------------------------------------------
# AC-104：分支根目录有 FRICTION-LOG.md，10/14 至 10/19 每天至少一条带日期的条目。
# ---------------------------------------------------------------------------
# awk 程序放在函数里的 here-doc 中（不放进 $(...)，避免 bash 3.2 在命令替换里误判引号）。
awk_friction() {
  cat <<'AWK'
BEGIN { n = split(dates, want, " "); cur = "" }
function isdate(s) { return s ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/ }
{
  line = $0
  sub(/\r$/, "", line)
  if (line ~ /^[ \t]*#/) {
    cur = ""
    h = line
    sub(/^[ \t]*#+[ \t]*/, "", h)
    sub(/^\*\*/, "", h)
    if (isdate(substr(h, 1, 10))) cur = substr(h, 1, 10)
    next
  }
  if (line ~ /^[ \t]*$/) next
  b = line
  sub(/^[ \t]*\|[ \t]*/, "", b)
  sub(/^[ \t]*([-*+]|[0-9]+\.)[ \t]+/, "", b)
  sub(/^\*\*/, "", b)
  if (isdate(substr(b, 1, 10))) {
    rest = substr(b, 11)
    gsub(/[ \t*:|.,;()-]/, "", rest)
    if (rest != "") seen[substr(b, 1, 10)] = 1
    next
  }
  if (cur != "") seen[cur] = 1
}
END {
  m = 0
  for (i = 1; i <= n; i++) if (!(want[i] in seen)) { print "FRICTION-LOG.md: no entry for " want[i]; m++ }
  exit (m > 0) ? 1 : 0
}
AWK
}

check_friction() {
  local until=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --until)
        if [ "$#" -lt 2 ]; then usage; return 2; fi
        until="$2"; shift 2 ;;
      *) usage; return 2 ;;
    esac
  done
  if [ -n "$until" ] && ! [[ "$until" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "checks.sh friction: --until wants YYYY-MM-DD, got '$until'" >&2
    return 2
  fi
  if [ ! -f FRICTION-LOG.md ]; then
    report friction "FRICTION-LOG.md: missing at the branch root"
    return 1
  fi
  local wanted=() d
  for d in "${FRICTION_DATES[@]}"; do
    if [ -z "$until" ] || [[ ! "$d" > "$until" ]]; then wanted+=("$d"); fi
  done
  if [ "${#wanted[@]}" -eq 0 ]; then
    report friction ""
    return 0
  fi
  local out rc=0
  out="$(awk -v dates="${wanted[*]}" "$(awk_friction)" FRICTION-LOG.md)" || rc=$?
  if [ "$rc" -gt 1 ]; then return 2; fi
  report friction "$out"
}

run_all() {
  local friction_args=()
  if [ "$#" -gt 0 ]; then
    if [ "$1" = "--until" ] && [ "$#" -eq 2 ]; then friction_args=("$@"); else usage; return 2; fi
  fi
  local failed=0 rc c
  for c in trademarks services audio hardcoded secrets friction; do
    rc=0
    if [ "$c" = friction ]; then
      check_friction ${friction_args[@]+"${friction_args[@]}"} || rc=$?
    else
      "check_$c" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
      echo "checks.sh: $c failed (exit $rc)" >&2
      if [ "$rc" -eq 2 ]; then failed=2; elif [ "$failed" -eq 0 ]; then failed=1; fi
    fi
  done
  return "$failed"
}

usage() {
  echo "usage: $0 <trademarks|services|audio|hardcoded|secrets|friction [--until YYYY-MM-DD]|all [--until YYYY-MM-DD]>" >&2
}

main() {
  if [ "$#" -lt 1 ]; then usage; return 2; fi
  local cmd="$1"; shift
  case "$cmd" in
    trademarks|services|audio|hardcoded|secrets|friction) "check_$cmd" "$@" ;;
    all) run_all "$@" ;;
    -h|--help) usage; return 0 ;;
    *) usage; return 2 ;;
  esac
}

main "$@"
