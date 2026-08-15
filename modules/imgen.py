"""Image generation code."""
from io import BytesIO
from pathlib import Path
import typing
import logging

from PIL import Image, ImageDraw, ImageFont, ImageOps

import discord

import requests

from modules import image_storage

logger = logging.getLogger('polybot.' + __name__)


class ImageFetchError(RuntimeError):
    """Raised when a remote image cannot be retrieved."""


def fetch_image(source: typing.Union[str, Path]) -> Image.Image:
    """Load an image from a trusted local path or retrieve it from a URL."""

    if isinstance(source, Path):
        logger.debug(f'fetch_image local path {source}')
        try:
            with Image.open(source) as local_image:
                local_image.load()
                return local_image.convert("RGBA")
        except (OSError, ValueError) as exc:
            raise ImageFetchError(f'Could not load local image from {source}') from exc

    url = str(source)

    headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9'
        }
    
    logger.debug(f'fetch_image {url}')
    try:
        # Do not let an unresponsive image host hold a worker thread indefinitely.
        response = requests.get(url, headers=headers, timeout=(2, 5))
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning('Unable to fetch image from %s: %s', url, exc)
        raise ImageFetchError(f'Could not retrieve image from {url}') from exc

    logger.debug(f'Status code {response.status_code}')
    try:
        with Image.open(BytesIO(response.content)) as remote_image:
            remote_image.load()
            return remote_image.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ImageFetchError(f'Could not decode image from {url}') from exc


def get_player_summary(member: discord.Member) -> str:
    """Get a summary for a player."""
    from modules.models import Player

    player = Player.get_or_except(
        player_string=member.id, guild_id=member.guild.id
    )
    local_wins, local_losses = player.get_record()
    local_elo = player.elo_moonrise
    global_wins, global_losses = player.discord_member.get_record()
    global_elo = player.discord_member.elo_moonrise
    return (
        f'LOCAL\n  {local_elo} ELO\n  {local_wins} W / {local_losses} L\n'
        f'GLOBAL\n  {global_elo} ELO\n  {global_wins} W / {global_losses} L'
    )


def draw_text(
        image: Image.Image, text: str, *, size: int = 50, left: int = 0,
        top: int = 0, colour: str = None):
    """Draw some text."""
    # load the font
    # font = ImageFont.truetype(
    #     'res/font.ttf', size,
    #     layout_engine=ImageFont.Layout.BASIC
    # )
    font = ImageFont.truetype(
        'res/font.ttf', size, layout_engine=ImageFont.Layout.BASIC
    )
    # draw the text
    draw = ImageDraw.Draw(image)
    draw.text((left, top), text, colour or '#fff', font)


def draw_inverse_text(
        image: Image.Image, text: str, *, size: int = 50, left: int = 0,
        top: int = 0):
    """Draw transparent text on a white background."""
    font = ImageFont.truetype(
        'res/font.ttf', size, layout_engine=ImageFont.Layout.BASIC
    )
    left_bbox, top_bbox, right_bbox, bottom_bbox = font.getbbox(text)
    width, height = right_bbox - left_bbox, bottom_bbox - top_bbox
    mask = Image.new('1', (width + 40, height + 30))
    ImageDraw.Draw(mask).text(
        (5 - left_bbox, 10 - top_bbox), text, fill='#fff', font=font
    )
    mask_data = list(mask.get_flattened_data())
    for idx, px in enumerate(mask_data):
        if px > 128:
            mask_data[idx] = 0
        else:
            mask_data[idx] = 255
    mask.putdata(mask_data)
    white = Image.new('RGBA', (width + 40, height + 30), '#fff')
    image.paste(white, (left, top), mask)


def get_text_width(text: str, font_size: int) -> int:
    """Get the width of some text."""
    font = ImageFont.truetype(
        'res/font.ttf', font_size, layout_engine=ImageFont.Layout.BASIC
    )
    left, _top, right, _bottom = font.getbbox(text)
    return right - left


def paste_image(
        base: Image.Image, image: Image.Image, left: int = 0, top: int = 0,
        height: int = 0):
    """Resize an image and paste it onto another."""
    # calculate the width
    start_w, start_h = image.size
    factor = start_h / height
    width = int(start_w / factor)
    # resize and paste the image
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    if image.mode != 'RGBA':
        image.putalpha(255)
    base.paste(image, (left, top), image)


def paste_image_contained(
        base: Image.Image, image: Image.Image, *, left: int, top: int,
        width: int, height: int):
    """Preserve aspect ratio while centering an image inside a fixed box."""
    if width <= 0 or height <= 0:
        raise ValueError('Image box dimensions must be positive.')

    image = ImageOps.contain(
        image, (width, height), method=Image.Resampling.LANCZOS
    )
    if image.mode != 'RGBA':
        image.putalpha(255)

    paste_left = left + (width - image.width) // 2
    paste_top = top + (height - image.height) // 2
    base.paste(image, (paste_left, paste_top), image)


def generate_gradient(
        colour1: str, colour2: str, width: int, height: int) -> Image:
    """Generate a vertical gradient."""
    base = Image.new('RGBA', (width, height), colour1)
    top = Image.new('RGBA', (width, height), colour2)
    mask = Image.new('L', (width, height))
    mask_data = []
    for y in range(height):
        for x in range(width):
            # mask_data.append(int(255 * ((x + y) / (height + width))))
            mask_data.append(int(75 * ((x + y) / (height + width))))
    mask.putdata(mask_data)
    base.paste(top, (0, 0), mask)
    return base


def rectangle(
        image: Image.Image, top: int, left: int, width: int,
        height: int, colour: str):
    """Draw a rectangle."""
    layer = Image.new('RGBA', (width, height), colour)
    mask = Image.new('L', (width, height), 128)
    image.paste(layer, (top, left), mask)


def draw_arrow(
        image: Image.Image, x_pos: int, y_pos: int, direction: str, fill: str):
    """Draw an arrow."""
    points = [(0, 45), (45, 0), (90, 45), (75, 60), (45, 30), (15, 60)]
    move = lambda x, y: (x + x_pos - 45, y + y_pos - 30)
    if direction == 'u':
        transform = lambda x, y: (x, y)
    elif direction == 'd':
        transform = lambda x, y: (x, 90 - y)
    elif direction == 'l':
        transform = lambda x, y: (y, x)
    elif direction == 'r':
        transform = lambda x, y: (90 - y, x)
    first_points = [move(*transform(*point)) for point in points]
    ImageDraw.Draw(image).polygon(first_points, fill=fill, outline=fill)
    second = lambda x, y: (x, y + 35)
    second_points = [move(*transform(*second(*point))) for point in points]
    ImageDraw.Draw(image).polygon(second_points, fill=fill, outline=fill)


def store_image(image: Image.Image, filename: str) -> discord.File:
    """Prepare an image to be sent over discord."""
    stream = BytesIO()
    image.save(stream, format='PNG')
    stream.seek(0)
    return discord.File(stream, filename=filename)


def player_draft_card(
        member: discord.Member, team_role: discord.Role,
        selecting_string: str = None) -> discord.File:
    """Generate a player draft card image."""
    from modules.models import Team

    # get the relevant images and strings
    team = Team.get_or_except(team_name=team_role.name, guild_id=member.guild.id)
    team_logo = fetch_image(image_storage.resolve_image('team', team))
    team_colour = str(team_role.colour)
    selecting_string = selecting_string if selecting_string else team.name
    title = f'{selecting_string.upper()} SELECT'
    # player_avatar = fetch_image(str(member.avatar_url_as(
    #     format='png', size=256
    # )))
    # if not member.avatar:
    #     player_avatar = player_avatar.convert("RGB")
    player_avatar = fetch_image(str(member.display_avatar.replace(size=256, format='png')))
    name = member.name.upper()
    summary = get_player_summary(member)
    wordmark = Image.open('res/pc_wordmark.png')
    # generate the image
    width = max(
        get_text_width(title, 50) + 125,
        get_text_width(name, 40) + 298,
        600
    )
    # im = generate_gradient('#4e459d', '#b03045', width, 400)
    im = generate_gradient(str(team_role.color), '#FFFFFF', width, 400)
    rectangle(im, 0, 0, width, 90, team_colour)
    if 'LIGHTNING' in title or 'PLAGUE' in title:
        text_colour = '#000'
        wordmark = Image.open('res/pc_wordmark_black.png')
    else:
        text_colour = None

    draw_text(im, title, left=120, top=15, size=50, colour=text_colour)
    draw_text(im, name, left=293, top=95, size=40, colour=text_colour)
    draw_text(im, summary, left=293, top=145, size=25, colour=text_colour)
    paste_image_contained(
        im, team_logo, left=20, top=10, width=100, height=80
    )
    paste_image(im, player_avatar, left=23, top=108, height=255)
    paste_image(im, wordmark, left=5, top=365, height=30)
    return store_image(im, f'{team_role.name}_selects_{member.name}.png')


def arrow_card(
        top_text: str, bottom_text: str, left_image: str, right_image: str,
        arrows: typing.Iterable[typing.Iterable[str]]):
    """Create a card that can be used for promotions or similar.

    "arrows" should be a list of tuples. The tuples should be a direction
    ("l", "r", "u" or "d" for left, right, up or down) followed by a hex code.
    For example:

        [('l', '#ff0000'), ('r', '#00ff00')]
    """
    logger.debug(f'arrow_card left_image: {left_image} right_image {right_image} arrows: {arrows}')
    # Create the base with a background gradient.
    height, width = 573, 836
    im = generate_gradient('#4e459d', '#b03045', width, height)
    draw = ImageDraw.Draw(im)
    # Put the wordmark in the top left.
    wordmark = Image.open('res/pc_wordmark.png')
    paste_image(im, wordmark, left=7, top=7, height=80)
    # Add an outline to the image.
    draw.rectangle([5, 5, width - 5, height - 5], width=2)
    # Draw the top text.
    top_text_left = (width - get_text_width(top_text, 70) - 15) // 2
    draw_inverse_text(im, top_text, left=top_text_left, top=100, size=70)
    # Draw fixed, symmetric image boxes and contain each image within its box.
    image_box_top = 236
    image_box_size = 230
    left_image_box = 100
    right_image_box = width - 100 - image_box_size
    for image_box_left in (left_image_box, right_image_box):
        draw.rectangle([
            image_box_left - 2,
            image_box_top - 2,
            image_box_left + image_box_size + 1,
            image_box_top + image_box_size + 1,
        ], width=2)

    paste_image_contained(
        im, fetch_image(left_image), left=left_image_box,
        top=image_box_top, width=image_box_size, height=image_box_size,
    )
    paste_image_contained(
        im, fetch_image(right_image), left=right_image_box,
        top=image_box_top, width=image_box_size, height=image_box_size,
    )
    # Draw the bottom text.
    bottom_text_left = (width - get_text_width(bottom_text, 70)) // 2
    draw_text(im, bottom_text, left=bottom_text_left, top=487, size=70)
    # Draw the arrows.
    next_arrow_y = 413 - (67 * len(arrows))
    for direction, colour in arrows:
        draw_arrow(im, width // 2, next_arrow_y, direction, colour)
        next_arrow_y += 135
    return store_image(im, f'{top_text}_{bottom_text}.png')
