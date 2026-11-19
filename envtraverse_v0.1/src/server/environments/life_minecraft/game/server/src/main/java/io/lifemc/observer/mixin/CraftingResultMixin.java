package io.lifemc.observer.mixin;

import io.lifemc.observer.ObserverMod;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.screen.slot.CraftingResultSlot;
import net.minecraft.server.network.ServerPlayerEntity;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Observe vanilla's accumulated crafted amount before vanilla resets it.
 * Both inventory/table crafting and shift-click output use this path. Merely
 * previewing a recipe, moving an item or receiving /give does not count.
 */
@Mixin(CraftingResultSlot.class)
public abstract class CraftingResultMixin {
    @Shadow @Final private PlayerEntity player;
    @Shadow private int amount;

    @Inject(method = "onCrafted(Lnet/minecraft/item/ItemStack;)V", at = @At("HEAD"))
    private void lifemc$crafted(ItemStack stack, CallbackInfo ci) {
        if (amount > 0 && player instanceof ServerPlayerEntity serverPlayer) {
            ObserverMod.crafted(serverPlayer, stack, amount);
        }
    }
}
