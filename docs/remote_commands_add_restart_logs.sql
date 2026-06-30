-- Adiciona as ações 'restart', 'logs' e 'update' à tabela remote_commands
-- Execute no SQL Editor do banco de dados

alter table public.remote_commands
  drop constraint if exists remote_commands_action_check;

alter table public.remote_commands
  add constraint remote_commands_action_check
  check (action in ('on', 'off', 'test', 'restart', 'logs', 'update'));
