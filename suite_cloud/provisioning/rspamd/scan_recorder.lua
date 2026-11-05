-- Suite Cloud: one JSON line per message a signed-in sender sends, for the outbound scan
-- dashboards. suite-cloud-scans.timer sends the lines to Suite Cloud. Written by
-- install-rspamd.yml: do not edit here.
local rspamd_logger = require "rspamd_logger"
local rspamd_util = require "rspamd_util"
local ucl = require "ucl"

local SPOOL = "/var/lib/rspamd/suite-cloud-scans.jsonl"
local LINE_BUFFER = 65536 -- one write per line, so the proxy processes never interleave lines

local function first_address(addresses)
  if addresses and addresses[1] and addresses[1].addr then
    return addresses[1].addr
  end
  return nil
end

local function get_symbol_scores(task)
  local scores = {}
  for _, symbol in ipairs(task:get_symbols_all() or {}) do
    scores[symbol.name] = symbol.score
  end
  return scores
end

local function get_record(task, user)
  local ip = task:get_from_ip()
  local content = task:get_content()
  return {
    time = rspamd_util.get_time(),
    user = user,
    envelope_from = first_address(task:get_from('smtp')),
    header_from = first_address(task:get_from('mime')),
    message_id = task:get_message_id(),
    queue_id = task:get_queue_id(),
    client_ip = (ip and ip:is_valid()) and tostring(ip) or nil,
    recipients = #(task:get_recipients('smtp') or {}),
    size = content and content:len() or 0,
    action = task:get_metric_action(),
    score = task:get_metric_score()[1],
    scan_time = math.floor(task:get_scan_time() * 1000),
    symbols = get_symbol_scores(task),
  }
end

local function record_scan(task)
  local user = task:get_user()
  if not user then
    return
  end
  local file, problem = io.open(SPOOL, 'a')
  if not file then
    rspamd_logger.errx(task, 'cannot open %s: %s', SPOOL, problem)
    return
  end
  file:setvbuf('full', LINE_BUFFER)
  file:write(ucl.to_format(get_record(task, user), 'json-compact'), '\n')
  file:close()
end

rspamd_config:register_symbol({
  name = 'SUITE_CLOUD_SCAN_RECORD',
  type = 'idempotent',
  callback = record_scan,
  flags = 'empty,explicit_disable,ignore_passthrough',
})
