-- Status do canal de comandos remotos por unidade (atualizado junto com as métricas, a cada 15 min)
-- Execute no SQL Editor do banco de dados

alter table public.pi_system_logs
  add column if not exists realtime_ok boolean null,          -- canal Realtime conectado e respondendo
  add column if not exists realtime_reconnects integer null,  -- reconexões desde o boot do serviço
  add column if not exists command_poll_ok boolean null;      -- busca periódica de comandos pendentes funcionando

-- Unidades com problema no recebimento de comandos:
-- select site_id, realtime_ok, command_poll_ok, realtime_reconnects, logged_at
-- from public.pi_system_logs
-- where command_poll_ok is not true or logged_at < now() - interval '30 minutes'
-- order by logged_at;
