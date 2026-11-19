package io.lifemc.observer;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.entity.event.v1.ServerEntityWorldChangeEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerEntityCombatEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerLivingEntityEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerPlayerEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.minecraft.entity.Entity;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.boss.dragon.EnderDragonEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.world.World;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/** Reads game objects on the server thread. Does not expose commands or alter gameplay. */
public final class ObserverMod implements ModInitializer {
    private static final Logger LOG = LoggerFactory.getLogger("lifemc-observer");
    private static final Gson JSON = new Gson();
    private static ObserverMod instance;
    private final Path directory = Path.of(env("TELEMETRY_DIR", "/telemetry"));
    private final String playerName = env("PLAYER_NAME", "AgentPlayer");
    private final String runId = env("RUN_ID", "default");
    private final String sessionId = UUID.randomUUID().toString();
    private final Map<String, Integer> kills = new HashMap<>();
    private final Map<String, Integer> crafted = new HashMap<>();
    private final Set<String> visitedDimensions = new HashSet<>();
    private final Map<UUID, JsonObject> dragonCredits = new HashMap<>();
    private final Set<UUID> completedDragons = new HashSet<>();
    private MinecraftServer server;
    private long seq;
    private int deaths, respawns, disconnects, dragonDefeats, dragonKills;
    private boolean priorDragonKill;
    private boolean telemetryError;

    @Override public void onInitialize() {
        instance = this;
        ServerLifecycleEvents.SERVER_STARTED.register(s -> {
            server = s;
            ServerWorld end = s.getWorld(World.END);
            priorDragonKill = end != null && end.getEnderDragonFight() != null
                    && end.getEnderDragonFight().hasPreviouslyKilled();
            try { Files.createDirectories(directory); }
            catch (IOException e) { throw new IllegalStateException("Cannot create telemetry directory", e); }
            event("server_started", new JsonObject());
            snapshot(false);
        });
        ServerLifecycleEvents.SERVER_STOPPING.register(s -> {
            event("server_stopped", new JsonObject());
            snapshot(true);
        });
        ServerTickEvents.END_SERVER_TICK.register(s -> {
            if (s.getTicks() % 10 == 0) snapshot(false);
        });
        ServerPlayConnectionEvents.JOIN.register((handler, sender, s) -> {
            if (matches(handler.player)) {
                visitedDimensions.add(handler.player.getWorld().getRegistryKey().getValue().toString());
                event("join", identity(handler.player));
                snapshot(false);
            }
        });
        ServerPlayConnectionEvents.DISCONNECT.register((handler, s) -> {
            if (matches(handler.player)) {
                disconnects++;
                event("disconnect", identity(handler.player));
                snapshot(false, true);
            }
        });
        ServerPlayerEvents.AFTER_RESPAWN.register((oldPlayer, newPlayer, alive) -> {
            if (matches(newPlayer)) {
                JsonObject data = identity(newPlayer);
                data.addProperty("after_death", !alive);
                if (!alive) respawns++;
                event("respawn", data);
                snapshot(false);
            }
        });
        ServerEntityWorldChangeEvents.AFTER_PLAYER_CHANGE_WORLD.register((player, origin, destination) -> {
            if (matches(player)) {
                JsonObject data = identity(player);
                data.addProperty("from_dimension", origin.getRegistryKey().getValue().toString());
                data.addProperty("dimension", destination.getRegistryKey().getValue().toString());
                data.add("inventory", inventory(player));
                visitedDimensions.add(destination.getRegistryKey().getValue().toString());
                event("dimension_change", data);
                snapshot(false);
            }
        });
        ServerEntityCombatEvents.AFTER_KILLED_OTHER_ENTITY.register((world, killer, victim) -> {
            if (killer instanceof ServerPlayerEntity player && matches(player)) {
                String type = Registries.ENTITY_TYPE.getId(victim.getType()).toString();
                kills.merge(type, 1, Integer::sum);
                if (victim instanceof EnderDragonEntity) dragonCredits.put(victim.getUuid(), identity(player));
                JsonObject data = identity(victim);
                data.addProperty("entity_type", type);
                data.addProperty("dimension", world.getRegistryKey().getValue().toString());
                data.addProperty("credited_to_player", true);
                data.add("credited_entity", identity(player));
                event("kill", data);
            }
        });
        ServerLivingEntityEvents.AFTER_DEATH.register((entity, source) -> {
            if (server == null) return;
            LivingEntity credited = entity.getPrimeAdversary();
            String type = Registries.ENTITY_TYPE.getId(entity.getType()).toString();
            JsonObject data = identity(entity);
            data.addProperty("entity_type", type);
            data.addProperty("dimension", entity.getWorld().getRegistryKey().getValue().toString());
            data.addProperty("prime_adversary_is_player", credited instanceof ServerPlayerEntity p && matches(p));
            data.add("credited_entity", credited == null ? JsonNull.INSTANCE : identity(credited));
            data.add("direct_attacker", source.getAttacker() == null ? JsonNull.INSTANCE : identity(source.getAttacker()));
            if (entity instanceof ServerPlayerEntity p && matches(p)) {
                deaths++;
                event("death", data);
            } else event("entity_death", data);
        });
    }

    /** Called only for items actually removed from a player's crafting output slot. */
    public static void crafted(ServerPlayerEntity player, ItemStack stack, int count) {
        ObserverMod mod = instance;
        if (mod == null || mod.server == null || !mod.matches(player) || count <= 0) return;
        String id = Registries.ITEM.getId(stack.getItem()).toString();
        if (id.equals("minecraft:air")) return;
        mod.crafted.merge(id, count, Integer::sum);
        JsonObject data = identity(player);
        data.addProperty("item", id);
        data.addProperty("count", count);
        data.addProperty("dimension", player.getWorld().getRegistryKey().getValue().toString());
        mod.event("craft", data);
    }

    public static void dragonCompleted(EnderDragonEntity dragon) {
        ObserverMod mod = instance;
        if (mod == null || mod.server == null || !mod.completedDragons.add(dragon.getUuid())) return;
        JsonObject credited = mod.dragonCredits.remove(dragon.getUuid());
        boolean playerKill = credited != null;
        mod.dragonDefeats++;
        if (playerKill) mod.dragonKills++;
        JsonObject data = identity(dragon);
        data.addProperty("entity_type", "minecraft:ender_dragon");
        data.addProperty("dimension", dragon.getWorld().getRegistryKey().getValue().toString());
        data.addProperty("credited_to_player", playerKill);
        data.add("credited_entity", credited == null ? JsonNull.INSTANCE : credited);
        data.addProperty("credit_basis", playerKill ? "vanilla_kill_event" : "unattributed");
        mod.event("dragon_defeated", data);
    }

    private JsonObject base() {
        JsonObject value = new JsonObject();
        value.addProperty("schema_version", 1);
        value.addProperty("run_id", runId);
        value.addProperty("session_id", sessionId);
        value.addProperty("time_ms", System.currentTimeMillis());
        value.addProperty("tick", server == null ? 0 : server.getTicks());
        return value;
    }

    private void snapshot(boolean stopped) { snapshot(stopped, false); }

    private void snapshot(boolean stopped, boolean disconnected) {
        if (server == null) return;
        JsonObject state = base();
        state.addProperty("last_event_seq", seq);
        state.addProperty("server_running", !stopped);
        state.addProperty("telemetry_error", telemetryError);
        state.addProperty("expected_player", playerName);
        state.addProperty("world_save", server.getSaveProperties().getLevelName());
        state.addProperty("difficulty", server.getSaveProperties().getDifficulty().getName());
        state.addProperty("hardcore", server.isHardcore());
        state.addProperty("dragon_previously_killed_at_start", priorDragonKill);
        ServerPlayerEntity player = stopped || disconnected ? null : server.getPlayerManager().getPlayer(playerName);
        state.addProperty("connected", player != null);
        if (player == null) state.add("player", JsonNull.INSTANCE);
        else {
            JsonObject data = identity(player);
            data.addProperty("dimension", player.getWorld().getRegistryKey().getValue().toString());
            data.addProperty("health", player.getHealth());
            JsonObject position = new JsonObject();
            position.addProperty("x", player.getX());
            position.addProperty("y", player.getY());
            position.addProperty("z", player.getZ());
            data.add("position", position);
            data.addProperty("yaw", player.getYaw());
            data.addProperty("pitch", player.getPitch());
            data.addProperty("alive", player.isAlive());
            data.addProperty("food", player.getHungerManager().getFoodLevel());
            data.addProperty("game_mode", player.interactionManager.getGameMode().getName());
            data.addProperty("selected_slot", player.getInventory().selectedSlot);
            data.add("inventory", inventory(player));
            data.add("cursor_stack", item(player.currentScreenHandler.getCursorStack()));
            state.add("player", data);
        }
        JsonObject counters = new JsonObject();
        counters.addProperty("deaths", deaths);
        counters.addProperty("disconnects", disconnects);
        counters.addProperty("respawns", respawns);
        counters.addProperty("dragon_defeats", dragonDefeats);
        counters.addProperty("dragon_kills", dragonKills);
        counters.add("kills", JSON.toJsonTree(kills));
        counters.add("crafted", JSON.toJsonTree(crafted));
        counters.add("visited_dimensions", JSON.toJsonTree(visitedDimensions));
        state.add("counters", counters);
        try {
            Path pending = directory.resolve("state.json.tmp");
            Files.writeString(pending, JSON.toJson(state) + "\n", StandardCharsets.UTF_8);
            try { Files.move(pending, directory.resolve("state.json"), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING); }
            catch (AtomicMoveNotSupportedException e) { Files.move(pending, directory.resolve("state.json"), StandardCopyOption.REPLACE_EXISTING); }
        } catch (IOException e) {
            telemetryError = true;
            LOG.error("Cannot write evaluation snapshot", e);
        }
    }

    private void event(String type, JsonObject data) {
        JsonObject value = base();
        value.addProperty("seq", ++seq);
        value.addProperty("type", type);
        value.add("data", data);
        try { Files.writeString(directory.resolve("events.jsonl"), JSON.toJson(value) + "\n", StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND); }
        catch (IOException e) {
            telemetryError = true;
            LOG.error("Cannot append evaluation event {}", type, e);
        }
    }

    private static JsonArray inventory(ServerPlayerEntity player) {
        JsonArray inventory = new JsonArray();
        for (int slot = 0; slot < player.getInventory().size(); slot++) {
            ItemStack stack = player.getInventory().getStack(slot);
            if (!stack.isEmpty()) {
                JsonObject item = item(stack);
                item.addProperty("slot", slot);
                item.addProperty("section", slot < 36 ? "main" : slot < 40 ? "armor" : "offhand");
                inventory.add(item);
            }
        }
        return inventory;
    }

    private static JsonObject item(ItemStack stack) {
        JsonObject item = new JsonObject();
        item.addProperty("item", Registries.ITEM.getId(stack.getItem()).toString());
        item.addProperty("count", stack.getCount());
        item.addProperty("damage", stack.getDamage());
        return item;
    }
    private static JsonObject identity(Entity entity) {
        JsonObject data = new JsonObject();
        data.addProperty("uuid", entity.getUuidAsString());
        data.addProperty("name", entity.getName().getString());
        return data;
    }
    private boolean matches(ServerPlayerEntity player) { return player.getGameProfile().getName().equalsIgnoreCase(playerName); }
    private static String env(String name, String fallback) { return System.getenv().getOrDefault(name, fallback); }
}
