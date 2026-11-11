--[[
  pipabobik — автозапуск музичного бота разом з OBS
  OBS → Інструменти → Скрипти → «+» → оберіть цей файл.

  Скрипт:
    • запускає бота при старті OBS (або при старті трансляції — на вибір);
    • повідомляє боту, коли трансляція почалась / закінчилась
      (музика грає лише під час стріму, після нього — пауза, черга зберігається);
    • закриває бота разом з OBS.
  Зв'язок з ботом — через файл data/obs_state (без вікон і консолей).
]]

obs = obslua

local bot_dir = ""
local mode = "obs"
local launched = false
local is_windows = package.config:sub(1, 1) == "\\"

local function sep() return is_windows and "\\" or "/" end

local function path(...)
	return table.concat({ ... }, sep())
end

local function file_exists(p)
	local f = io.open(p, "r")
	if f then f:close() return true end
	return false
end

local function write_state(state)
	if bot_dir == "" then return end
	local f = io.open(path(bot_dir, "data", "obs_state"), "w")
	if f then
		f:write(state .. " " .. os.time())
		f:close()
	else
		obs.script_log(obs.LOG_WARNING, "pipabobik: не вдалося записати data/obs_state — перевірте папку бота")
	end
end

local function launch(live)
	write_state(live and "live" or "idle") -- прибрати можливий «exit» від кнопки «Зупинити»
	if bot_dir == "" then
		obs.script_log(obs.LOG_WARNING, "pipabobik: вкажіть папку бота в налаштуваннях скрипта")
		return
	end
	local args = "--obs" .. (live and " --live" or "")
	local cmd
	if is_windows then
		local pyw = path(bot_dir, ".venv", "Scripts", "pythonw.exe")
		if not file_exists(pyw) then
			obs.script_log(obs.LOG_WARNING, "pipabobik: не знайдено " .. pyw .. " — запустіть install.bat")
			return
		end
		-- start повертається одразу; pythonw працює без вікна консолі
		cmd = string.format('start "" /D "%s" "%s" bot.py %s', bot_dir, pyw, args)
	else
		local py = path(bot_dir, ".venv", "bin", "python")
		cmd = string.format('cd "%s" && nohup "%s" bot.py %s >/dev/null 2>&1 &', bot_dir, py, args)
	end
	os.execute(cmd)
	launched = true
	obs.script_log(obs.LOG_INFO, "pipabobik: бот запущено (" .. args .. ")")
end

local function on_event(event)
	if event == obs.OBS_FRONTEND_EVENT_STREAMING_STARTED then
		write_state("live")
		if mode == "stream" or not launched then
			launch(true) -- якщо бот уже працює, нова копія сама тихо закриється
		end
	elseif event == obs.OBS_FRONTEND_EVENT_STREAMING_STOPPED then
		write_state("idle")
	elseif event == obs.OBS_FRONTEND_EVENT_EXIT then
		write_state("exit")
	end
end

function script_description()
	return [[<h2>🎀 pipabobik</h2>
<p>Автозапуск музичного бота. Вкажіть папку, куди ви розпакували бота
(там, де лежить <code>bot.py</code>), і оберіть, коли запускати.</p>
<p>Віджет: <code>http://localhost:17777/widget</code></p>]]
end

function script_properties()
	local p = obs.obs_properties_create()
	obs.obs_properties_add_path(p, "bot_dir", "Папка бота", obs.OBS_PATH_DIRECTORY, "", nil)
	local list = obs.obs_properties_add_list(p, "mode", "Коли запускати бота",
		obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
	obs.obs_property_list_add_string(list, "Разом з OBS (музика — з початком трансляції)", "obs")
	obs.obs_property_list_add_string(list, "Разом з трансляцією", "stream")
	obs.obs_properties_add_button(p, "launch_now", "Запустити бота зараз", function()
		launch(obs.obs_frontend_streaming_active())
		return false
	end)
	obs.obs_properties_add_button(p, "stop_now", "Зупинити бота", function()
		write_state("exit")
		launched = false
		obs.script_log(obs.LOG_INFO, "pipabobik: бот зупиняється")
		return false
	end)
	return p
end

function script_defaults(settings)
	obs.obs_data_set_default_string(settings, "mode", "obs")
end

function script_update(settings)
	bot_dir = obs.obs_data_get_string(settings, "bot_dir")
	if is_windows then bot_dir = bot_dir:gsub("/", "\\") end
	mode = obs.obs_data_get_string(settings, "mode")
end

function script_load(settings)
	script_update(settings)
	obs.obs_frontend_add_event_callback(on_event)
	if obs.obs_frontend_streaming_active() then
		write_state("live")
		launch(true)
	else
		write_state("idle")
		if mode == "obs" then launch(false) end
	end
end

function script_unload()
	write_state("exit")
end
