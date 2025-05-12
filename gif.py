from PIL import Image, ImageDraw, ImageFont

def create_precise_centered_countdown(filename, start_number, frames_per_second=10, size=(800, 800), font_size=360):
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except:
        font = ImageFont.load_default()

    total_frames = start_number * frames_per_second
    duration_per_frame = int(1000 / frames_per_second)  # milliseconds

    frames = []
    for frame_index in range(total_frames):
        current_second = start_number - frame_index // frames_per_second
        progress_ratio = (frame_index + 1) / total_frames

        img = Image.new("RGBA", size, (255, 255, 255, 0))  # Transparent background
        draw = ImageDraw.Draw(img)

        # Draw number
        text = str(current_second)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = (size[0] - text_width) // 2
        text_y = (size[1] - text_height) // 2 - bbox[1]  # Adjust vertical baseline
        position = (text_x, text_y)
        draw.text(position, text, fill=(0, 0, 0, 255), font=font)

        # Draw background circle and arc
        center = (size[0] // 2, size[1] // 2)
        radius = 300
        arc_width = 30

        draw.ellipse(
            [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
            outline=(220, 220, 220, 255),
            width=arc_width
        )

        start_angle = -90
        end_angle = -90 + (progress_ratio * 360)
        draw.arc(
            [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
            start=start_angle,
            end=end_angle,
            fill=(0, 0, 0, 255),
            width=arc_width
        )

        frames.append(img)

    frames[0].save(
        filename,
        save_all=True,
        append_images=frames[1:],
        duration=duration_per_frame,
        loop=0,
        transparency=0,
        disposal=2,
        dpi=(600, 600)
    )

# Generate high-quality, smooth countdowns
create_precise_centered_countdown("countdown_10s_smooth.gif", start_number=10)
create_precise_centered_countdown("countdown_3s_smooth.gif", start_number=3)
create_precise_centered_countdown("countdown_15s_smooth.gif", start_number=15)