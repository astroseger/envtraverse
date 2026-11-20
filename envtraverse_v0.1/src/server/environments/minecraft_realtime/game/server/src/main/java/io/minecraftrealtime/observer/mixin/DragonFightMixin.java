package io.minecraftrealtime.observer.mixin;

import io.minecraftrealtime.observer.ObserverMod;
import net.minecraft.entity.boss.dragon.EnderDragonEntity;
import net.minecraft.entity.boss.dragon.EnderDragonFight;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import java.util.UUID;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Observe fight completion, including the dragon's delayed death animation. */
@Mixin(EnderDragonFight.class)
public abstract class DragonFightMixin {
    @Shadow private UUID dragonUuid;
    @Inject(method = "dragonKilled", at = @At("TAIL"))
    private void minecraftRealtime$dragonKilled(EnderDragonEntity dragon, CallbackInfo ci) {
        if (dragon.getUuid().equals(dragonUuid)) ObserverMod.dragonCompleted(dragon);
    }
}
