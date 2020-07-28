"""Image generation code."""
from io import BytesIO

import discord

import requests

from PIL import Image, ImageDraw, ImageFont

from modules.models import Player, Team


def fetch_image(url: str) -> Image:
    """Get an image from a URL."""
    response = requests.get(url)
    return Image.open(BytesIO(response.content))


def get_player_summary(member: discord.Member) -> str:
    """Get a summary for a player."""
    player, _upserted = Player.get_by_discord_id(
        member.id, member.guild.id
    )
    if not player:
        raise ValueError('That player is not registered.')
    local_wins, local_losses = player.get_record()
    local_elo = player.elo
    global_wins, global_losses = player.discord_member.get_record()
    global_elo = player.discord_member.elo
    return (
        f'LOCAL\n  {local_elo} ELO\n  {local_wins} W / {local_losses} L\n'
        f'GLOBAL\n  {global_elo} ELO\n  {local_wins} W / {local_losses} L'
    )


def draw_text(
        image: Image.Image, text: str, *, size: int = 50, left: int = 0,
        top: int = 0):
    """Draw some text."""
    # load the font
    font = ImageFont.truetype(
        f'res/font.ttf', size,
        layout_engine=ImageFont.LAYOUT_BASIC
    )
    # draw the text
    draw = ImageDraw.Draw(image)
    draw.text((left, top), text, '#fff', font)


def get_text_width(text: str, font_size: int) -> int:
    """Get the width of some text."""
    return ImageFont.truetype(
        f'res/font.ttf', font_size,
        layout_engine=ImageFont.LAYOUT_BASIC
    ).getsize(text)[0]


def paste_image(
        base: Image.Image, image: Image.Image, left: int = 0, top: int = 0,
        height: int = 0):
    """Resize an image and paste it onto another."""
    # calculate the width
    start_w, start_h = image.size
    factor = start_h / height
    width = int(start_w / factor)
    # resize and paste the image
    image = image.resize((width, height))
    base.paste(image, (left, top), image)


def generate_gradient(
        colour1: str, colour2: str, width: int, height: int) -> Image:
    """Generate a vertical gradient."""
    base = Image.new('RGBA', (width, height), colour1)
    top = Image.new('RGBA', (width, height), colour2)
    mask = Image.new('L', (width, height))
    mask_data = []
    for y in range(height):
        for x in range(width):
            mask_data.append(int(255 * ((x + y) / (height + width))))
    mask.putdata(mask_data)
    base.paste(top, (0, 0), mask)
    return base


def rectangle(
        image: Image.Image, top: int, left: int, width: int,
        height: int, colour: str):
    """Draw a rectangle."""
    layer = Image.new('RGBA', (width, height), colour)
    mask = Image.new('L', (width, height), 128)
    image.paste(layer, (0, 0), mask)


def player_draft_card(
        member: discord.Member, team_role: discord.Role) -> discord.File:
    """Generate a player draft card image."""
    # get the relevant images and strings
    team = Team.get_by_name(team_role.name, member.guild.id)
    team_logo = fetch_image(team.image_url)
    team_colour = str(team_role.colour)
    title = f'{team_role.name.upper()} SELECT'
    player_avatar = fetch_image(str(member.avatar_url) + '?size=256')
    name = member.name.upper()
    summary = get_player_summary(member)
    wordmark = Image.open('res/pc_wordmark.png')
    filename = f'{team_role.name}_selects_{member.name}.png'
    # generate the image
    width = max(
        get_text_width(title, 50) + 125,
        get_text_width(name, 40) + 298,
        600
    )
    im = generate_gradient('#4e459d', '#b03045', width, 400)
    rectangle(im, 0, 0, width, 90, team_colour)
    draw_text(im, title, left=120, top=15, size=50)
    draw_text(im, name, left=293, top=95, size=40)
    draw_text(im, summary, left=293, top=145, size=25)
    paste_image(im, team_logo, left=20, top=10, height=80)
    paste_image(im, player_avatar, left=23, top=108, height=255)
    paste_image(im, wordmark, left=5, top=365, height=30)
    # store the image
    stream = BytesIO()
    im.save(stream, format='PNG')
    stream.seek(0)
    return discord.File(stream, filename=filename)
