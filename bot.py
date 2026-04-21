async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Передаём именно update, а не query!
    if data == 'farm':
        await farm(update, context)
    elif data == 'balance':
        await balance(update, context)
    elif data == 'craft':
        await craft(update, context)
    elif data == 'smoke':
        await smoke(update, context)
    elif data == 'ritual':
        await ritual(update, context)
    elif data == 'status':
        await status(update, context)
    elif data == 'top':
        await top(update, context)
    elif data == 'guild_info':
        await guild_info(update, context)
    elif data == 'rules':
        await rules(update, context)
