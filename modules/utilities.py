from discord.ext import commands


async def get_guild_member(ctx, input):

        # Find matching Guild member by @Mention or Name. Fall back to case-insensitive search
        # TODO: support Username#Discriminator (ie an @mention without the @)

        guild_matches, substring_matches = [], []
        try:
            guild_matches.append(await commands.MemberConverter().convert(ctx, input))
        except commands.errors.BadArgument:
            pass
            # No matches in standard MemberConverter. Move on to a case-insensitive search.
            for p in ctx.guild.members:
                name_str = p.nick.upper() + p.name.upper() if p.nick else p.name.upper()
                if p.name.upper() == input.upper():
                    guild_matches.append(p)
                if input.upper() in name_str:
                    substring_matches.append(p)

            if len(guild_matches) > 0:
                return guild_matches
            if len(input) > 2:
                return substring_matches

        return guild_matches


def get_matching_roles(discord_member, list_of_role_names):
        # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.polytopia_id
        member_roles = [x.name for x in discord_member.roles]
        return set(member_roles).intersection(list_of_role_names)
